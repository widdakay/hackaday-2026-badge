// FTM ranging node with ESP-NOW discovery.
//
// Every node runs the same firmware and is symmetric:
//   * a hidden softAP on kChannel with the FTM responder enabled
//   * an ESP-NOW beacon every second announcing "I'm a ranging node"
//   * a ranging loop that measures, in turn, every node heard recently
//   * an ESP-NOW broadcast of every measurement, so a node plugged into a
//     laptop also reports what the other nodes measured
//
// ESP-NOW is sent on the AP interface, so the sender MAC of every packet is
// that node's AP MAC, which is also the address its FTM responder answers on.
//
// Serial output (USB), one record per line, comma separated:
//   SELF,<ap_mac>,<name>,<channel>
//   PEER,<ap_mac>,<name>,<rssi>              first time a node is heard
//   PEERS,<ap_mac>,<name>,<rssi>,<age_ms>    every 10 s, one line per live peer
//   LOST,<ap_mac>                            not heard for kPeerTimeoutMs
//   DIST,<from>,<to>,<status>,<dist_cm>,<rtt_raw_ns>,<rtt_est_ns>,<rssi>,<entries>,
//        <rtt_med_ps>,<rtt_mean_ps>,<rtt_min_ps>,<rtt_max_ps>
//        measured by this node (from == SELF)
//   RDIST,...same fields...
//        measured by another node, received over ESP-NOW
//
// dist_cm, rtt_raw_ns and rtt_est_ns come from the Wi-Fi driver and are whole
// nanoseconds of round trip (15 cm steps). The *_ps fields are computed here
// from the per-frame round-trip times in picoseconds. Each frame is quantized
// to 1.5625 ns (640 MHz) and negative frames are clamped to 0, so rtt_mean_ps
// is the useful one: the driver's average without its rounding.
// One-way distance = rtt_ps * 0.1499 mm.
#include <algorithm>
#include <cinttypes>
#include <cstdio>
#include <cstring>

#include "driver/gpio.h"
#include "esp_event.h"
#include "esp_idf_version.h"
#include "esp_log.h"
#include "esp_mac.h"
#include "esp_now.h"
#include "esp_timer.h"
#include "esp_wifi.h"
#include "freertos/FreeRTOS.h"
#include "freertos/event_groups.h"
#include "freertos/queue.h"
#include "freertos/semphr.h"
#include "freertos/task.h"
#include "nvs_flash.h"

namespace {

// ---- Configuration --------------------------------------------------------
constexpr uint8_t kChannel = 6;             // every node must share one channel
constexpr uint8_t kFrameCount = 16;         // FTM frames per session (16/24/32/64)
constexpr uint16_t kBurstPeriod = 2;        // FTM burst period, 100 ms units
constexpr uint32_t kBeaconPeriodMs = 1000;
constexpr uint32_t kRangeGapMs = 150;       // pause between ranging sessions
constexpr int64_t kPeerTimeoutMs = 10000;
constexpr int kMaxPeers = 24;
#if CONFIG_IDF_TARGET_ESP32C3
constexpr gpio_num_t kLed = GPIO_NUM_8;     // SuperMini blue LED, active low
constexpr bool kLedActiveLow = true;
#else
constexpr gpio_num_t kLed = GPIO_NUM_NC;    // set for other boards if wanted
constexpr bool kLedActiveLow = false;
#endif

// ---- Wire protocol (ESP-NOW payloads) ---------------------------------------
constexpr char kMagic[4] = {'B', 'B', 'F', 'T'};
constexpr uint8_t kProtoVersion = 2;
constexpr int kMaxReportEntries = 64;
enum : uint8_t { kMsgBeacon = 1, kMsgRange = 2 };

struct __attribute__((packed)) Header {
    char magic[4];
    uint8_t version;
    uint8_t type;
};

struct __attribute__((packed)) BeaconMsg {
    Header hdr;
    uint32_t seq;
    char name[16];
};

struct __attribute__((packed)) Range {
    uint8_t status;       // wifi_ftm_status_t
    uint8_t entries;      // valid per-frame readings
    int8_t rssi;
    uint32_t dist_cm;
    uint32_t rtt_raw_ns;
    uint32_t rtt_est_ns;
    uint32_t rtt_med_ps;
    uint32_t rtt_mean_ps;
    uint32_t rtt_min_ps;
    uint32_t rtt_max_ps;
};

struct __attribute__((packed)) RangeMsg {
    Header hdr;
    uint8_t to[6];        // responder that was measured (sender is the initiator)
    Range range;
};

// ---- State ------------------------------------------------------------------
struct Peer {
    bool used;
    uint8_t mac[6];
    char name[17];
    int8_t rssi;
    int64_t last_seen_ms;
};

struct RxPacket {
    uint8_t src[6];
    int8_t rssi;
    uint8_t len;
    uint8_t data[64];
};

constexpr EventBits_t kFtmDone = BIT0;
const uint8_t kBroadcast[6] = {0xff, 0xff, 0xff, 0xff, 0xff, 0xff};

EventGroupHandle_t g_events;
QueueHandle_t g_rx_queue;
SemaphoreHandle_t g_peers_lock;
Peer g_peers[kMaxPeers];
wifi_event_ftm_report_t g_report;
uint8_t g_self[6];
char g_name[17];

int64_t now_ms() { return esp_timer_get_time() / 1000; }

void led(bool on) {
    if (kLed != GPIO_NUM_NC) gpio_set_level(kLed, on != kLedActiveLow);
}

void flash_led(uint32_t ms) {
    led(true);
    vTaskDelay(pdMS_TO_TICKS(ms));
    led(false);
}

void fill_header(Header &h, uint8_t type) {
    std::memcpy(h.magic, kMagic, sizeof(kMagic));
    h.version = kProtoVersion;
    h.type = type;
}

void print_dist(const char *tag, const uint8_t *from, const uint8_t *to, const Range &r) {
    if (r.status == FTM_STATUS_SUCCESS) {
        printf("%s," MACSTR "," MACSTR ",ok,%" PRIu32 ",%" PRIu32 ",%" PRIu32 ",%d,%u,%" PRIu32 ",%" PRIu32
               ",%" PRIu32 ",%" PRIu32 "\n",
               tag, MAC2STR(from), MAC2STR(to), r.dist_cm, r.rtt_raw_ns, r.rtt_est_ns, r.rssi, r.entries,
               r.rtt_med_ps, r.rtt_mean_ps, r.rtt_min_ps, r.rtt_max_ps);
    } else {
        printf("%s," MACSTR "," MACSTR ",fail_%u,,,,%d,0,,,,\n", tag, MAC2STR(from), MAC2STR(to), r.status, r.rssi);
    }
}

// Per-frame round-trip stats in picoseconds from the driver's FTM report.
// Also frees the driver's copy of the report.
void frame_stats(uint8_t num_entries, Range &out) {
    static wifi_ftm_report_entry_t entries[kMaxReportEntries];
    uint8_t n = std::min<int>(num_entries, kMaxReportEntries);
    if (n == 0 || esp_wifi_ftm_get_report(entries, n) != ESP_OK) {
        esp_wifi_ftm_get_report(nullptr, 0);
        return;
    }
    uint32_t rtts[kMaxReportEntries];
    int valid = 0;
    uint64_t sum = 0;
    for (int i = 0; i < n; i++) {
        if (entries[i].rtt == UINT32_MAX) continue;
        rtts[valid++] = entries[i].rtt;
        sum += entries[i].rtt;
    }
    if (!valid) return;
    std::sort(rtts, rtts + valid);
    out.entries = valid;
    out.rtt_med_ps = valid % 2 ? rtts[valid / 2] : (rtts[valid / 2 - 1] + rtts[valid / 2]) / 2;
    out.rtt_mean_ps = sum / valid;
    out.rtt_min_ps = rtts[0];
    out.rtt_max_ps = rtts[valid - 1];
}

// ---- Wi-Fi / ESP-NOW callbacks (run in the Wi-Fi task: keep them short) ------
void on_wifi_event(void *, esp_event_base_t, int32_t id, void *data) {
    if (id == WIFI_EVENT_FTM_REPORT) {
        g_report = *static_cast<wifi_event_ftm_report_t *>(data);
        xEventGroupSetBits(g_events, kFtmDone);
    }
}

void on_espnow_recv(const esp_now_recv_info_t *info, const uint8_t *data, int len) {
    if (len < static_cast<int>(sizeof(Header)) || len > 64) return;
    if (std::memcmp(data, kMagic, sizeof(kMagic)) != 0) return;
    RxPacket pkt;
    std::memcpy(pkt.src, info->src_addr, 6);
    pkt.rssi = info->rx_ctrl->rssi;
    pkt.len = len;
    std::memcpy(pkt.data, data, len);
    xQueueSend(g_rx_queue, &pkt, 0);  // drop if full
}

#if ESP_IDF_VERSION >= ESP_IDF_VERSION_VAL(5, 5, 0)
void on_espnow_sent(const esp_now_send_info_t *, esp_now_send_status_t) {}
#else
void on_espnow_sent(const uint8_t *, esp_now_send_status_t) {}
#endif

// ---- Peer table -------------------------------------------------------------
void peer_heard(const uint8_t *mac, const char *name, int8_t rssi) {
    xSemaphoreTake(g_peers_lock, portMAX_DELAY);
    Peer *slot = nullptr;
    for (auto &p : g_peers) {
        if (p.used && std::memcmp(p.mac, mac, 6) == 0) { slot = &p; break; }
    }
    bool is_new = false;
    if (!slot) {
        for (auto &p : g_peers) {
            if (!p.used) { slot = &p; is_new = true; break; }
        }
    }
    if (slot) {
        slot->used = true;
        std::memcpy(slot->mac, mac, 6);
        if (name) {
            std::strncpy(slot->name, name, 16);
            slot->name[16] = '\0';
        }
        slot->rssi = rssi;
        slot->last_seen_ms = now_ms();
        if (is_new) printf("PEER," MACSTR ",%s,%d\n", MAC2STR(mac), slot->name, rssi);
    }
    xSemaphoreGive(g_peers_lock);
}

// Pick the next live peer after index `after`, round robin. Returns -1 if none.
int next_peer(int after, uint8_t *mac_out) {
    int found = -1;
    xSemaphoreTake(g_peers_lock, portMAX_DELAY);
    int64_t t = now_ms();
    for (auto &p : g_peers) {
        if (p.used && t - p.last_seen_ms > kPeerTimeoutMs) {
            printf("LOST," MACSTR "\n", MAC2STR(p.mac));
            p.used = false;
        }
    }
    for (int i = 1; i <= kMaxPeers; i++) {
        int idx = (after + i) % kMaxPeers;
        if (g_peers[idx].used) {
            std::memcpy(mac_out, g_peers[idx].mac, 6);
            found = idx;
            break;
        }
    }
    xSemaphoreGive(g_peers_lock);
    return found;
}

// ---- Tasks ------------------------------------------------------------------
void rx_task(void *) {
    RxPacket pkt;
    while (true) {
        if (!xQueueReceive(g_rx_queue, &pkt, portMAX_DELAY)) continue;
        const auto *hdr = reinterpret_cast<const Header *>(pkt.data);
        if (hdr->version != kProtoVersion) continue;
        if (hdr->type == kMsgBeacon && pkt.len >= sizeof(BeaconMsg)) {
            const auto *b = reinterpret_cast<const BeaconMsg *>(pkt.data);
            char name[17];
            std::memcpy(name, b->name, 16);
            name[16] = '\0';
            peer_heard(pkt.src, name, pkt.rssi);
        } else if (hdr->type == kMsgRange && pkt.len >= sizeof(RangeMsg)) {
            const auto *r = reinterpret_cast<const RangeMsg *>(pkt.data);
            peer_heard(pkt.src, nullptr, pkt.rssi);
            Range range;
            std::memcpy(&range, &r->range, sizeof(range));
            print_dist("RDIST", pkt.src, r->to, range);
        }
    }
}

void beacon_task(void *) {
    BeaconMsg msg = {};
    fill_header(msg.hdr, kMsgBeacon);
    std::strncpy(msg.name, g_name, sizeof(msg.name));
    int n = 0;
    while (true) {
        msg.seq++;
        esp_now_send(kBroadcast, reinterpret_cast<const uint8_t *>(&msg), sizeof(msg));
        if (n++ % 10 == 0) {
            printf("SELF," MACSTR ",%s,%d\n", MAC2STR(g_self), g_name, kChannel);
            xSemaphoreTake(g_peers_lock, portMAX_DELAY);
            int64_t t = now_ms();
            for (auto &p : g_peers) {
                if (p.used) {
                    printf("PEERS," MACSTR ",%s,%d,%" PRId64 "\n", MAC2STR(p.mac), p.name, p.rssi,
                           t - p.last_seen_ms);
                }
            }
            xSemaphoreGive(g_peers_lock);
        }
        vTaskDelay(pdMS_TO_TICKS(kBeaconPeriodMs));
    }
}

void range_one(const uint8_t *peer) {
    wifi_ftm_initiator_cfg_t cfg = {};
    std::memcpy(cfg.resp_mac, peer, 6);
    cfg.channel = kChannel;
    cfg.frm_count = kFrameCount;
    cfg.burst_period = kBurstPeriod;
    cfg.use_get_report_api = true;

    xEventGroupClearBits(g_events, kFtmDone);
    if (esp_wifi_ftm_initiate_session(&cfg) != ESP_OK) {
        printf("DIST," MACSTR "," MACSTR ",start_err,,,,,0\n", MAC2STR(g_self), MAC2STR(peer));
        return;
    }
    if (!(xEventGroupWaitBits(g_events, kFtmDone, pdTRUE, pdFALSE, pdMS_TO_TICKS(3000)) & kFtmDone)) {
        esp_wifi_ftm_end_session();
        printf("DIST," MACSTR "," MACSTR ",timeout,,,,,0\n", MAC2STR(g_self), MAC2STR(peer));
        return;
    }
    wifi_event_ftm_report_t r = g_report;
    Range range = {};
    range.status = static_cast<uint8_t>(r.status);
    range.entries = r.ftm_report_num_entries;
    range.dist_cm = r.dist_est;
    range.rtt_raw_ns = r.rtt_raw;
    range.rtt_est_ns = r.rtt_est;
    frame_stats(r.ftm_report_num_entries, range);

    xSemaphoreTake(g_peers_lock, portMAX_DELAY);
    for (auto &p : g_peers) {
        if (p.used && std::memcmp(p.mac, peer, 6) == 0) range.rssi = p.rssi;
    }
    xSemaphoreGive(g_peers_lock);

    print_dist("DIST", g_self, peer, range);

    RangeMsg msg = {};
    fill_header(msg.hdr, kMsgRange);
    std::memcpy(msg.to, peer, 6);
    msg.range = range;
    esp_now_send(kBroadcast, reinterpret_cast<const uint8_t *>(&msg), sizeof(msg));

    if (r.status == FTM_STATUS_SUCCESS) flash_led(20);
}

void range_task(void *) {
    int idx = -1;
    uint8_t peer[6];
    while (true) {
        int next = next_peer(idx, peer);
        if (next < 0) {
            vTaskDelay(pdMS_TO_TICKS(500));
            continue;
        }
        idx = next;
        range_one(peer);
        vTaskDelay(pdMS_TO_TICKS(kRangeGapMs));
    }
}

// ---- Setup ------------------------------------------------------------------
void start_radio() {
    ESP_ERROR_CHECK(esp_netif_init());
    ESP_ERROR_CHECK(esp_event_loop_create_default());
    esp_netif_create_default_wifi_ap();
    esp_netif_create_default_wifi_sta();

    wifi_init_config_t init = WIFI_INIT_CONFIG_DEFAULT();
    ESP_ERROR_CHECK(esp_wifi_init(&init));
    ESP_ERROR_CHECK(esp_event_handler_register(WIFI_EVENT, WIFI_EVENT_FTM_REPORT, on_wifi_event, nullptr));
    ESP_ERROR_CHECK(esp_wifi_set_storage(WIFI_STORAGE_RAM));
    ESP_ERROR_CHECK(esp_wifi_set_mode(WIFI_MODE_APSTA));

    esp_read_mac(g_self, ESP_MAC_WIFI_SOFTAP);
    snprintf(g_name, sizeof(g_name), "node-%02X%02X%02X", g_self[3], g_self[4], g_self[5]);

    wifi_config_t ap = {};
    std::strncpy(reinterpret_cast<char *>(ap.ap.ssid), g_name, sizeof(ap.ap.ssid));
    ap.ap.ssid_len = std::strlen(g_name);
    ap.ap.ssid_hidden = 1;
    ap.ap.channel = kChannel;
    ap.ap.authmode = WIFI_AUTH_OPEN;
    ap.ap.max_connection = 1;
    ap.ap.ftm_responder = true;
    ESP_ERROR_CHECK(esp_wifi_set_config(WIFI_IF_AP, &ap));
    ESP_ERROR_CHECK(esp_wifi_start());
    ESP_ERROR_CHECK(esp_wifi_set_ps(WIFI_PS_NONE));

    ESP_ERROR_CHECK(esp_now_init());
    ESP_ERROR_CHECK(esp_now_register_recv_cb(on_espnow_recv));
    ESP_ERROR_CHECK(esp_now_register_send_cb(on_espnow_sent));
    esp_now_peer_info_t bcast = {};
    std::memcpy(bcast.peer_addr, kBroadcast, 6);
    bcast.channel = kChannel;
    bcast.ifidx = WIFI_IF_AP;
    bcast.encrypt = false;
    ESP_ERROR_CHECK(esp_now_add_peer(&bcast));
}

}  // namespace

extern "C" void app_main() {
    esp_err_t err = nvs_flash_init();
    if (err == ESP_ERR_NVS_NO_FREE_PAGES || err == ESP_ERR_NVS_NEW_VERSION_FOUND) {
        ESP_ERROR_CHECK(nvs_flash_erase());
        err = nvs_flash_init();
    }
    ESP_ERROR_CHECK(err);
    esp_log_level_set("wifi", ESP_LOG_ERROR);  // the FTM code logs every session at WARN

    if (kLed != GPIO_NUM_NC) {
        gpio_reset_pin(kLed);
        gpio_set_direction(kLed, GPIO_MODE_OUTPUT);
        led(false);
    }

    g_events = xEventGroupCreate();
    g_rx_queue = xQueueCreate(32, sizeof(RxPacket));
    g_peers_lock = xSemaphoreCreateMutex();

    start_radio();
    printf("SELF," MACSTR ",%s,%d\n", MAC2STR(g_self), g_name, kChannel);

    xTaskCreate(rx_task, "ftm_rx", 4096, nullptr, 5, nullptr);
    xTaskCreate(beacon_task, "ftm_beacon", 3072, nullptr, 4, nullptr);
    xTaskCreate(range_task, "ftm_range", 4096, nullptr, 4, nullptr);
}

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
//   CSI,<from>,<to>,<t_us>,<rssi>,<noise_floor>,<sig_mode>,<rate_or_mcs>,<cwb>,<stbc>,
//       <first_word_invalid>,<kind>,<seq>,<len>,<csi_base64>
//        channel state of one packet received from a peer (only when kCsiEnabled).
//        to is this node (or ff:ff:.. for broadcasts), t_us is the radio's receive
//        timestamp. kind is beacon, range, ping, ftm or other (what the packet
//        was); seq is the beacon/ping sequence number, empty otherwise. The
//        CSI bytes are int8 (imaginary, real) pairs per subcarrier, as the
//        driver gives them: on HT20, 128 bytes of LLTF (subcarriers 0..31 then
//        -32..-1), followed for HT packets by 128 bytes of HT-LTF in the same
//        order (and 128 more of STBC HT-LTF2 for STBC packets).
//   CSISTAT,<seen>,<queue_drops>,<throttled>,<printed>
//        every 10 s, counts since boot: CSI callbacks from known peers, dropped
//        because the queue was full, skipped by the per-peer rate limit, printed
//
// dist_cm, rtt_raw_ns and rtt_est_ns come from the Wi-Fi driver and are whole
// nanoseconds of round trip (15 cm steps). The *_ps fields are computed here
// from the per-frame round-trip times in picoseconds. Each frame is quantized
// to 1.5625 ns (640 MHz) and negative frames are clamped to 0, so rtt_mean_ps
// is the useful one: the driver's average without its rounding.
// One-way distance = rtt_ps * 0.1499 mm.
#include <algorithm>
#include <atomic>
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
constexpr uint16_t kBurstPeriod = 0;        // FTM burst period, 100 ms units; 0 = no preference (fastest), 1 is rejected
constexpr uint32_t kBeaconPeriodMs = 1000;
constexpr uint32_t kSessionTimeoutMs = 1000; // a session takes ~100-300 ms; give up on a busy peer fast
constexpr int64_t kPeerTimeoutMs = 10000;
constexpr int kMaxPeers = 24;

// CSI capture (firmware/CSI_PLAN.md). Needs CONFIG_ESP_WIFI_CSI_ENABLED.
constexpr bool kCsiEnabled = true;
constexpr uint32_t kCsiMaxHzPerPeer = 20;   // per peer and packet kind; USB serial carries ~100 kB/s
constexpr bool kCsiFtmFrames = true;        // also capture CSI of FTM frames, not only ESP-NOW
constexpr uint32_t kPingHz = 10;            // ESP-NOW ping broadcast rate, 0 = off; only sent with kCsiEnabled
constexpr bool kEspnowHtRate = true;        // send ESP-NOW at HT20 MCS0, so CSI includes the HT-LTF.
                                            // Turn off if far nodes are lost: MCS0 needs a few dB more SNR
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
enum : uint8_t { kMsgBeacon = 1, kMsgRange = 2, kMsgPing = 5 };  // 3, 4: badge NAMES, SYNC

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

// Sent kPingHz times a second only to give the other nodes packets to take CSI
// from. Receivers ignore it, apart from the CSI.
struct __attribute__((packed)) PingMsg {
    Header hdr;
    uint32_t seq;
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

// One CSI packet as copied out of the Wi-Fi task's callback.
enum : uint8_t { kCsiBeacon, kCsiRange, kCsiPing, kCsiFtm, kCsiOther, kCsiKinds };
const char *const kCsiKindNames[kCsiKinds] = {"beacon", "range", "ping", "ftm", "other"};
constexpr int kCsiMaxLen = 384;     // LLTF + HT-LTF + STBC HT-LTF2 on HT20

struct CsiPacket {
    uint8_t src[6];
    uint8_t dst[6];
    uint32_t t_us;
    int8_t rssi;
    int8_t noise_floor;
    uint8_t sig_mode;
    uint8_t rate;           // rx_ctrl.rate for non-HT, rx_ctrl.mcs for HT
    uint8_t cwb;
    uint8_t stbc;
    bool first_word_invalid;
    uint8_t kind;
    bool has_seq;
    uint32_t seq;
    uint16_t len;
    int8_t buf[kCsiMaxLen];
};

constexpr EventBits_t kFtmDone = BIT0;
constexpr EventBits_t kPeerNew = BIT1;   // a node was heard for the first time
const uint8_t kBroadcast[6] = {0xff, 0xff, 0xff, 0xff, 0xff, 0xff};

EventGroupHandle_t g_events;
QueueHandle_t g_rx_queue;
SemaphoreHandle_t g_peers_lock;
Peer g_peers[kMaxPeers];
wifi_event_ftm_report_t g_report;
uint8_t g_self[6];
char g_name[17];

QueueHandle_t g_csi_queue;
std::atomic<uint32_t> g_csi_seen{0}, g_csi_queue_drops{0};

int64_t now_ms() { return esp_timer_get_time() / 1000; }

void led(bool on) {
    if (kLed != GPIO_NUM_NC) gpio_set_level(kLed, on != kLedActiveLow);
}

// Turn the LED on and let a one-shot timer turn it off, so the ranging task
// doesn't sit in a delay for the flash.
esp_timer_handle_t g_led_timer;

void flash_led(uint32_t ms) {
    if (kLed == GPIO_NUM_NC) return;
    led(true);
    esp_timer_stop(g_led_timer);  // restart if still lit from the last flash
    esp_timer_start_once(g_led_timer, static_cast<uint64_t>(ms) * 1000);
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

// What a received frame was, from its body: one of our ESP-NOW messages
// (found by its magic in the vendor element), an FTM frame (public action,
// category 4, action 33), or something else.
uint8_t csi_kind(const wifi_csi_info_t *info, bool *has_seq, uint32_t *seq) {
    *has_seq = false;
    const uint8_t *p = info->payload;
    int n = p ? info->payload_len : 0;
    for (int i = 0; i + static_cast<int>(sizeof(Header)) <= n && i < 48; i++) {
        if (std::memcmp(p + i, kMagic, sizeof(kMagic)) != 0) continue;
        const auto *h = reinterpret_cast<const Header *>(p + i);
        if (h->type == kMsgBeacon || h->type == kMsgPing) {
            if (i + static_cast<int>(sizeof(Header) + 4) <= n) {
                std::memcpy(seq, p + i + sizeof(Header), 4);
                *has_seq = true;
            }
            return h->type == kMsgBeacon ? kCsiBeacon : kCsiPing;
        }
        return h->type == kMsgRange ? kCsiRange : kCsiOther;
    }
    // The payload may or may not include the 24-byte MAC header; try both.
    for (int off : {0, 24}) {
        if (off + 2 <= n && p[off] == 4 && p[off + 1] == 33) return kCsiFtm;
    }
    return kCsiOther;
}

bool is_peer(const uint8_t *mac) {
    // Unlocked: a torn read while peer_heard writes a slot only drops or keeps
    // one packet wrongly, and taking a mutex in the Wi-Fi task isn't allowed.
    for (const auto &p : g_peers) {
        if (p.used && std::memcmp(p.mac, mac, 6) == 0) return true;
    }
    return false;
}

void on_csi(void *, wifi_csi_info_t *info) {
    if (!info || !info->buf || !is_peer(info->mac)) return;
    g_csi_seen.fetch_add(1, std::memory_order_relaxed);
    bool has_seq;
    uint32_t seq = 0;
    uint8_t kind = csi_kind(info, &has_seq, &seq);
    if (kind == kCsiFtm && !kCsiFtmFrames) return;
    static CsiPacket pkt;  // only the Wi-Fi task runs this; too big for its stack
    std::memcpy(pkt.src, info->mac, 6);
    std::memcpy(pkt.dst, info->dmac, 6);
    const wifi_pkt_rx_ctrl_t &rx = info->rx_ctrl;
    pkt.t_us = rx.timestamp;
    pkt.rssi = rx.rssi;
    pkt.noise_floor = rx.noise_floor;
    pkt.sig_mode = rx.sig_mode;
    pkt.rate = rx.sig_mode ? rx.mcs : rx.rate;
    pkt.cwb = rx.cwb;
    pkt.stbc = rx.stbc;
    pkt.first_word_invalid = info->first_word_invalid;
    pkt.kind = kind;
    pkt.has_seq = has_seq;
    pkt.seq = seq;
    pkt.len = std::min<int>(info->len, kCsiMaxLen);
    std::memcpy(pkt.buf, info->buf, pkt.len);
    if (xQueueSend(g_csi_queue, &pkt, 0) != pdTRUE) g_csi_queue_drops.fetch_add(1, std::memory_order_relaxed);
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
    if (is_new) xEventGroupSetBits(g_events, kPeerNew);
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

// Base64 of `n` bytes into `out` (4 * ceil(n / 3) + 1 bytes). Returns its length.
int base64(const uint8_t *in, int n, char *out) {
    static const char kAlphabet[] = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";
    int o = 0;
    for (int i = 0; i < n; i += 3) {
        uint32_t v = in[i] << 16 | (i + 1 < n ? in[i + 1] << 8 : 0) | (i + 2 < n ? in[i + 2] : 0);
        out[o++] = kAlphabet[v >> 18 & 63];
        out[o++] = kAlphabet[v >> 12 & 63];
        out[o++] = i + 1 < n ? kAlphabet[v >> 6 & 63] : '=';
        out[o++] = i + 2 < n ? kAlphabet[v & 63] : '=';
    }
    out[o] = '\0';
    return o;
}

// Prints the CSI packets the callback queued, at most kCsiMaxHzPerPeer per
// peer and packet kind, and a CSISTAT line every 10 s.
void csi_task(void *) {
    struct Last {
        uint8_t mac[6];
        int64_t us[kCsiKinds];
    };
    static Last last[kMaxPeers];
    static int used = 0;
    static CsiPacket pkt;
    static char b64[4 * ((kCsiMaxLen + 2) / 3) + 1];
    const int64_t min_gap_us = 1000000 / kCsiMaxHzPerPeer;
    uint32_t throttled = 0, printed = 0;
    int64_t next_stat = esp_timer_get_time() + 10000000;
    while (true) {
        if (xQueueReceive(g_csi_queue, &pkt, pdMS_TO_TICKS(1000))) {
            int64_t t = esp_timer_get_time();
            Last *l = nullptr;
            for (int i = 0; i < used; i++) {
                if (std::memcmp(last[i].mac, pkt.src, 6) == 0) { l = &last[i]; break; }
            }
            if (!l) {
                // Table full: reuse the slot with the oldest packet.
                if (used < kMaxPeers) {
                    l = &last[used++];
                } else {
                    l = &last[0];
                    for (int i = 1; i < used; i++) {
                        if (*std::max_element(last[i].us, last[i].us + kCsiKinds) <
                            *std::max_element(l->us, l->us + kCsiKinds)) l = &last[i];
                    }
                }
                std::memcpy(l->mac, pkt.src, 6);
                std::fill(l->us, l->us + kCsiKinds, INT64_MIN / 2);
            }
            if (t - l->us[pkt.kind] < min_gap_us) {
                throttled++;
            } else {
                l->us[pkt.kind] = t;
                base64(reinterpret_cast<const uint8_t *>(pkt.buf), pkt.len, b64);
                char seq[11] = "";
                if (pkt.has_seq) snprintf(seq, sizeof(seq), "%" PRIu32, pkt.seq);
                printf("CSI," MACSTR "," MACSTR ",%" PRIu32 ",%d,%d,%u,%u,%u,%u,%d,%s,%s,%u,%s\n",
                       MAC2STR(pkt.src), MAC2STR(pkt.dst), pkt.t_us, pkt.rssi, pkt.noise_floor, pkt.sig_mode,
                       pkt.rate, pkt.cwb, pkt.stbc, pkt.first_word_invalid ? 1 : 0, kCsiKindNames[pkt.kind], seq,
                       pkt.len, b64);
                printed++;
            }
        }
        if (esp_timer_get_time() >= next_stat) {
            next_stat += 10000000;
            printf("CSISTAT,%" PRIu32 ",%" PRIu32 ",%" PRIu32 ",%" PRIu32 "\n", g_csi_seen.load(),
                   g_csi_queue_drops.load(), throttled, printed);
        }
    }
}

void ping_task(void *) {
    PingMsg msg = {};
    fill_header(msg.hdr, kMsgPing);
    TickType_t wake = xTaskGetTickCount();
    const TickType_t period = std::max<TickType_t>(1, pdMS_TO_TICKS(1000 / kPingHz));
    while (true) {
        msg.seq++;
        esp_now_send(kBroadcast, reinterpret_cast<const uint8_t *>(&msg), sizeof(msg));
        vTaskDelayUntil(&wake, period);
    }
}

// One FTM session with `peer`. Returns false if it could not be started.
bool range_one(const uint8_t *peer) {
    wifi_ftm_initiator_cfg_t cfg = {};
    std::memcpy(cfg.resp_mac, peer, 6);
    cfg.channel = kChannel;
    cfg.frm_count = kFrameCount;
    cfg.burst_period = kBurstPeriod;
    cfg.use_get_report_api = true;

    xEventGroupClearBits(g_events, kFtmDone);
    if (esp_wifi_ftm_initiate_session(&cfg) != ESP_OK) {
        printf("DIST," MACSTR "," MACSTR ",start_err,,,,,0\n", MAC2STR(g_self), MAC2STR(peer));
        return false;
    }
    if (!(xEventGroupWaitBits(g_events, kFtmDone, pdTRUE, pdFALSE, pdMS_TO_TICKS(kSessionTimeoutMs)) & kFtmDone)) {
        esp_wifi_ftm_end_session();
        printf("DIST," MACSTR "," MACSTR ",timeout,,,,,0\n", MAC2STR(g_self), MAC2STR(peer));
        return true;
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
    return true;
}

void range_task(void *) {
    int idx = -1;
    uint8_t peer[6];
    while (true) {
        int next = next_peer(idx, peer);
        if (next < 0) {
            // Sleep until a node turns up (peer_heard sets the bit; one set
            // before this wait just makes it return at once).
            xEventGroupWaitBits(g_events, kPeerNew, pdTRUE, pdFALSE, portMAX_DELAY);
            continue;
        }
        idx = next;
        // No gap between sessions: each one blocks on the driver's report.
        // Only a failed start returns without blocking, so give up the CPU
        // for one tick then, or a stuck driver would starve the idle task.
        if (!range_one(peer)) vTaskDelay(1);
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

    if (kEspnowHtRate) {
        esp_now_rate_config_t rate = {};
        rate.phymode = WIFI_PHY_MODE_HT20;
        rate.rate = WIFI_PHY_RATE_MCS0_LGI;
        esp_err_t err = esp_now_set_peer_rate_config(kBroadcast, &rate);
        if (err != ESP_OK) printf("ERR,espnow_rate,%s\n", esp_err_to_name(err));
    }

    if (kCsiEnabled) {
        wifi_csi_config_t csi = {};
        csi.lltf_en = true;
        csi.htltf_en = true;
        csi.stbc_htltf2_en = true;
        csi.ltf_merge_en = false;       // keep LLTF and HT-LTF separate, as measured
        csi.channel_filter_en = false;  // no smoothing across subcarriers
        csi.manu_scale = false;
        csi.shift = 0;
        csi.dump_ack_en = false;
        ESP_ERROR_CHECK(esp_wifi_set_csi_config(&csi));
        ESP_ERROR_CHECK(esp_wifi_set_csi_rx_cb(on_csi, nullptr));
        ESP_ERROR_CHECK(esp_wifi_set_csi(true));
    }
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
        esp_timer_create_args_t led_timer = {};
        led_timer.callback = [](void *) { led(false); };
        led_timer.name = "led";
        ESP_ERROR_CHECK(esp_timer_create(&led_timer, &g_led_timer));
    }

    g_events = xEventGroupCreate();
    g_rx_queue = xQueueCreate(32, sizeof(RxPacket));
    g_peers_lock = xSemaphoreCreateMutex();
    if (kCsiEnabled) g_csi_queue = xQueueCreate(16, sizeof(CsiPacket));

    start_radio();
    printf("SELF," MACSTR ",%s,%d\n", MAC2STR(g_self), g_name, kChannel);

    xTaskCreate(rx_task, "ftm_rx", 4096, nullptr, 5, nullptr);
    xTaskCreate(beacon_task, "ftm_beacon", 3072, nullptr, 4, nullptr);
    xTaskCreate(range_task, "ftm_range", 4096, nullptr, 4, nullptr);
    if (kCsiEnabled) {
        // Below ranging: printing CSI must never delay a session.
        xTaskCreate(csi_task, "ftm_csi", 4096, nullptr, 3, nullptr);
        if (kPingHz) xTaskCreate(ping_task, "ftm_ping", 2048, nullptr, 4, nullptr);
    }
}

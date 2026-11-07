// Wi-Fi FTM core, see ftm_core.h.
#include "ftm_core.h"

#include <algorithm>
#include <atomic>
#include <cstring>

#include "esp_event.h"
#include "esp_wifi.h"

namespace {

constexpr int kMaxEntries = 64;

// Session state, shared between the Wi-Fi event task and the caller.
std::atomic<int> g_state{FTM_CORE_IDLE};
wifi_event_ftm_report_t g_report;  // written by the event task before DONE is set
bool g_handler_registered = false;
std::atomic<void (*)(void)> g_done_cb{nullptr};

void on_ftm_report(void *, esp_event_base_t, int32_t, void *data) {
    if (g_state.load() != FTM_CORE_RUNNING) {
        esp_wifi_ftm_get_report(nullptr, 0);  // stray report: just free it
        return;
    }
    g_report = *static_cast<wifi_event_ftm_report_t *>(data);
    g_state.store(FTM_CORE_DONE);
    if (auto cb = g_done_cb.load()) cb();
}

// Per-frame round-trip stats from the driver's report; frees the driver's copy.
void frame_stats(uint8_t num_entries, ftm_result_t &out) {
    static wifi_ftm_report_entry_t entries[kMaxEntries];
    uint8_t n = std::min<int>(num_entries, kMaxEntries);
    if (n == 0 || esp_wifi_ftm_get_report(entries, n) != ESP_OK) {
        esp_wifi_ftm_get_report(nullptr, 0);
        return;
    }
    uint32_t rtts[kMaxEntries];
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

esp_err_t ensure_handler() {
    if (g_handler_registered) return ESP_OK;
    esp_err_t err = esp_event_handler_register(WIFI_EVENT, WIFI_EVENT_FTM_REPORT, on_ftm_report, nullptr);
    if (err == ESP_OK) g_handler_registered = true;
    return err;
}

}  // namespace

extern "C" int ftm_core_set_responder(int enable) {
    wifi_config_t cfg = {};
    esp_err_t err = esp_wifi_get_config(WIFI_IF_AP, &cfg);
    if (err != ESP_OK) return err;
    cfg.ap.ftm_responder = enable != 0;
    return esp_wifi_set_config(WIFI_IF_AP, &cfg);
}

extern "C" int ftm_core_start(const uint8_t mac[6], uint8_t channel, uint8_t frames, uint16_t burst_period) {
    esp_err_t err = ensure_handler();
    if (err != ESP_OK) return err;
    int expected = FTM_CORE_IDLE;
    if (!g_state.compare_exchange_strong(expected, FTM_CORE_RUNNING)) return ESP_ERR_INVALID_STATE;

    wifi_ftm_initiator_cfg_t cfg = {};
    std::memcpy(cfg.resp_mac, mac, 6);
    cfg.channel = channel;
    cfg.frm_count = frames;
    cfg.burst_period = burst_period;
    cfg.use_get_report_api = true;
    err = esp_wifi_ftm_initiate_session(&cfg);
    if (err != ESP_OK) g_state.store(FTM_CORE_IDLE);
    return err;
}

extern "C" int ftm_core_poll(ftm_result_t *out) {
    int state = g_state.load();
    if (state != FTM_CORE_DONE) return state;
    const wifi_event_ftm_report_t r = g_report;
    ftm_result_t res = {};
    std::memcpy(res.peer, r.peer_mac, 6);
    res.status = static_cast<uint8_t>(r.status);
    res.entries = r.ftm_report_num_entries;
    res.dist_cm = r.dist_est;
    res.rtt_raw_ns = r.rtt_raw;
    res.rtt_est_ns = r.rtt_est;
    frame_stats(r.ftm_report_num_entries, res);
    *out = res;
    g_state.store(FTM_CORE_IDLE);
    return FTM_CORE_DONE;
}

extern "C" int ftm_core_state(void) { return g_state.load(); }

extern "C" int ftm_core_cancel(void) {
    if (g_state.load() != FTM_CORE_RUNNING) return ESP_OK;
    esp_err_t err = esp_wifi_ftm_end_session();
    g_state.store(FTM_CORE_IDLE);
    return err;
}

extern "C" void ftm_core_set_done_cb(void (*cb)(void)) { g_done_cb.store(cb); }

// Wi-Fi FTM (802.11mc round-trip timing) core for the badge, C API.
//
// Implemented in C++ (ftm_core.cpp) on top of ESP-IDF; no MicroPython headers.
// The MicroPython binding is modftm.c. Wi-Fi must already be started (for
// example with network.WLAN(...).active(True) from Python).
#pragma once

#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

typedef struct {
    uint8_t peer[6];       // responder MAC the result belongs to
    uint8_t status;        // wifi_ftm_status_t, 0 = success
    uint8_t entries;       // valid per-frame readings
    uint32_t dist_cm;      // driver estimate, whole-ns round trip (15 cm steps)
    uint32_t rtt_raw_ns;
    uint32_t rtt_est_ns;
    uint32_t rtt_med_ps;   // per-frame round trip stats, picoseconds
    uint32_t rtt_mean_ps;  // the useful one: one-way cm = rtt_mean_ps * 0.0149896
    uint32_t rtt_min_ps;
    uint32_t rtt_max_ps;
} ftm_result_t;

enum {
    FTM_CORE_IDLE = 0,     // no session started since the last result was read
    FTM_CORE_RUNNING = 1,  // session in progress
    FTM_CORE_DONE = 2,     // result written to *out
};

// Turn the softAP's FTM responder on or off. Returns an esp_err_t.
int ftm_core_set_responder(int enable);

// Start a session with the responder at `mac` (its AP MAC) on `channel`.
// frames: 0, 16, 24, 32 or 64. burst_period: 100 ms units, 0 = no preference.
// Returns an esp_err_t; ESP_ERR_INVALID_STATE if a session is running.
int ftm_core_start(const uint8_t mac[6], uint8_t channel, uint8_t frames, uint16_t burst_period);

// Non-blocking. Returns FTM_CORE_IDLE, FTM_CORE_RUNNING or FTM_CORE_DONE
// (then *out holds the result and the core goes back to idle).
int ftm_core_poll(ftm_result_t *out);

// Current state without consuming a finished result.
int ftm_core_state(void);

// Abort a running session. Returns an esp_err_t.
int ftm_core_cancel(void);

// Called from the Wi-Fi event task when a session's result is ready (state
// becomes FTM_CORE_DONE). Keep it short and thread safe; NULL to clear.
void ftm_core_set_done_cb(void (*cb)(void));

#ifdef __cplusplus
}
#endif

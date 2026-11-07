// MicroPython binding for the FTM core (ftm_core.cpp).
//
//   import ftm
//   ftm.responder(True)                       # answer other nodes' measurements
//   ftm.start(mac, channel, frames=16, burst=2)   # mac: 6-byte responder AP MAC
//   r = ftm.poll()    # None while running/idle, else a result tuple:
//       (peer_mac, status, entries, dist_cm, rtt_raw_ns, rtt_est_ns,
//        rtt_med_ps, rtt_mean_ps, rtt_min_ps, rtt_max_ps)
//   ftm.busy()        # True while a session is running
//   ftm.cancel()
//
// Wi-Fi must be active first (network.WLAN(network.AP_IF).active(True)).
#include "py/runtime.h"
#include "py/mperrno.h"
#include "esp_err.h"

#include "ftm_core.h"

static void check(int err) {
    if (err != ESP_OK) {
        mp_raise_msg_varg(&mp_type_OSError, MP_ERROR_TEXT("ftm: %s"), esp_err_to_name(err));
    }
}

static mp_obj_t ftm_responder(size_t n_args, const mp_obj_t *args) {
    check(ftm_core_set_responder(n_args == 0 || mp_obj_is_true(args[0])));
    return mp_const_none;
}
static MP_DEFINE_CONST_FUN_OBJ_VAR_BETWEEN(ftm_responder_obj, 0, 1, ftm_responder);

static mp_obj_t ftm_start(size_t n_args, const mp_obj_t *pos_args, mp_map_t *kw_args) {
    enum { ARG_mac, ARG_channel, ARG_frames, ARG_burst };
    static const mp_arg_t allowed[] = {
        { MP_QSTR_mac, MP_ARG_REQUIRED | MP_ARG_OBJ, {.u_obj = MP_OBJ_NULL} },
        { MP_QSTR_channel, MP_ARG_REQUIRED | MP_ARG_INT, {.u_int = 0} },
        { MP_QSTR_frames, MP_ARG_INT, {.u_int = 16} },
        { MP_QSTR_burst, MP_ARG_INT, {.u_int = 2} },
    };
    mp_arg_val_t a[MP_ARRAY_SIZE(allowed)];
    mp_arg_parse_all(n_args, pos_args, kw_args, MP_ARRAY_SIZE(allowed), allowed, a);

    mp_buffer_info_t mac;
    mp_get_buffer_raise(a[ARG_mac].u_obj, &mac, MP_BUFFER_READ);
    if (mac.len != 6) {
        mp_raise_ValueError(MP_ERROR_TEXT("mac must be 6 bytes"));
    }
    check(ftm_core_start(mac.buf, a[ARG_channel].u_int, a[ARG_frames].u_int, a[ARG_burst].u_int));
    return mp_const_none;
}
static MP_DEFINE_CONST_FUN_OBJ_KW(ftm_start_obj, 2, ftm_start);

static mp_obj_t ftm_poll(void) {
    ftm_result_t r;
    if (ftm_core_poll(&r) != FTM_CORE_DONE) {
        return mp_const_none;
    }
    mp_obj_t items[] = {
        mp_obj_new_bytes(r.peer, 6),
        MP_OBJ_NEW_SMALL_INT(r.status),
        MP_OBJ_NEW_SMALL_INT(r.entries),
        mp_obj_new_int_from_uint(r.dist_cm),
        mp_obj_new_int_from_uint(r.rtt_raw_ns),
        mp_obj_new_int_from_uint(r.rtt_est_ns),
        mp_obj_new_int_from_uint(r.rtt_med_ps),
        mp_obj_new_int_from_uint(r.rtt_mean_ps),
        mp_obj_new_int_from_uint(r.rtt_min_ps),
        mp_obj_new_int_from_uint(r.rtt_max_ps),
    };
    return mp_obj_new_tuple(MP_ARRAY_SIZE(items), items);
}
static MP_DEFINE_CONST_FUN_OBJ_0(ftm_poll_obj, ftm_poll);

static mp_obj_t ftm_busy(void) {
    return mp_obj_new_bool(ftm_core_state() == FTM_CORE_RUNNING);
}
static MP_DEFINE_CONST_FUN_OBJ_0(ftm_busy_obj, ftm_busy);

static mp_obj_t ftm_cancel(void) {
    check(ftm_core_cancel());
    return mp_const_none;
}
static MP_DEFINE_CONST_FUN_OBJ_0(ftm_cancel_obj, ftm_cancel);

static const mp_rom_map_elem_t ftm_module_globals_table[] = {
    { MP_ROM_QSTR(MP_QSTR___name__), MP_ROM_QSTR(MP_QSTR_ftm) },
    { MP_ROM_QSTR(MP_QSTR_responder), MP_ROM_PTR(&ftm_responder_obj) },
    { MP_ROM_QSTR(MP_QSTR_start), MP_ROM_PTR(&ftm_start_obj) },
    { MP_ROM_QSTR(MP_QSTR_poll), MP_ROM_PTR(&ftm_poll_obj) },
    { MP_ROM_QSTR(MP_QSTR_busy), MP_ROM_PTR(&ftm_busy_obj) },
    { MP_ROM_QSTR(MP_QSTR_cancel), MP_ROM_PTR(&ftm_cancel_obj) },
    { MP_ROM_QSTR(MP_QSTR_STATUS_OK), MP_ROM_INT(0) },
};
static MP_DEFINE_CONST_DICT(ftm_module_globals, ftm_module_globals_table);

const mp_obj_module_t ftm_user_cmodule = {
    .base = { &mp_type_module },
    .globals = (mp_obj_dict_t *)&ftm_module_globals,
};

MP_REGISTER_MODULE(MP_QSTR_ftm, ftm_user_cmodule);

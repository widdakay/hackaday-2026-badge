// MicroPython binding for the layout core (graph_core.cpp).
//
//   import graphlayout
//   from array import array
//   xs = array('f', ...); ys = array('f', ...)        # start positions, n each
//   ei = array('H', ...); ej = array('H', ...)        # edge endpoints, m each
//   ed = array('f', ...)                              # edge lengths, metres
//   stress = graphlayout.solve(xs, ys, ei, ej, ed, iterations=200,
//                              restarts=0, margin=0.2, seed=1, mds=False)
//   # mds=True ignores the start in xs/ys and uses classical MDS instead
//   # xs, ys now hold the layout
//
// Blocks for the whole solve: about a millisecond for ten nodes with a few
// hundred iterations.
#include "py/runtime.h"

#include "graph_core.h"

static void get_buf(mp_obj_t obj, mp_buffer_info_t *info, char typecode, const char *name) {
    mp_get_buffer_raise(obj, info, MP_BUFFER_RW);
    if (info->typecode != typecode) {
        mp_raise_msg_varg(&mp_type_TypeError, MP_ERROR_TEXT("%s must be array('%c')"), name, typecode);
    }
}

static mp_obj_t graphlayout_solve(size_t n_args, const mp_obj_t *pos_args, mp_map_t *kw_args) {
    enum { ARG_xs, ARG_ys, ARG_ei, ARG_ej, ARG_ed, ARG_iterations, ARG_restarts, ARG_margin, ARG_seed, ARG_mds };
    static const mp_arg_t allowed[] = {
        { MP_QSTR_xs, MP_ARG_REQUIRED | MP_ARG_OBJ, {.u_obj = MP_OBJ_NULL} },
        { MP_QSTR_ys, MP_ARG_REQUIRED | MP_ARG_OBJ, {.u_obj = MP_OBJ_NULL} },
        { MP_QSTR_ei, MP_ARG_REQUIRED | MP_ARG_OBJ, {.u_obj = MP_OBJ_NULL} },
        { MP_QSTR_ej, MP_ARG_REQUIRED | MP_ARG_OBJ, {.u_obj = MP_OBJ_NULL} },
        { MP_QSTR_ed, MP_ARG_REQUIRED | MP_ARG_OBJ, {.u_obj = MP_OBJ_NULL} },
        { MP_QSTR_iterations, MP_ARG_INT, {.u_int = 200} },
        { MP_QSTR_restarts, MP_ARG_INT, {.u_int = 0} },
        { MP_QSTR_margin, MP_ARG_OBJ, {.u_obj = MP_OBJ_NULL} },
        { MP_QSTR_seed, MP_ARG_INT, {.u_int = 1} },
        { MP_QSTR_mds, MP_ARG_BOOL, {.u_bool = false} },
    };
    mp_arg_val_t a[MP_ARRAY_SIZE(allowed)];
    mp_arg_parse_all(n_args, pos_args, kw_args, MP_ARRAY_SIZE(allowed), allowed, a);

    mp_buffer_info_t xs, ys, ei, ej, ed;
    get_buf(a[ARG_xs].u_obj, &xs, 'f', "xs");
    get_buf(a[ARG_ys].u_obj, &ys, 'f', "ys");
    get_buf(a[ARG_ei].u_obj, &ei, 'H', "ei");
    get_buf(a[ARG_ej].u_obj, &ej, 'H', "ej");
    get_buf(a[ARG_ed].u_obj, &ed, 'f', "ed");
    size_t n = xs.len / sizeof(float);
    size_t m = ed.len / sizeof(float);
    if (ys.len != xs.len || ei.len / 2 != m || ej.len / 2 != m) {
        mp_raise_ValueError(MP_ERROR_TEXT("xs/ys and ei/ej/ed lengths must match"));
    }
    float margin = a[ARG_margin].u_obj == MP_OBJ_NULL ? 0.2f : mp_obj_get_float(a[ARG_margin].u_obj);

    float s = graph_core_solve((int)n, xs.buf, ys.buf, (int)m, ei.buf, ej.buf, ed.buf,
                               a[ARG_iterations].u_int, a[ARG_restarts].u_int, margin,
                               (uint32_t)a[ARG_seed].u_int, a[ARG_mds].u_bool);
    if (s < 0.0f) {
        mp_raise_ValueError(MP_ERROR_TEXT("too many nodes or edge index out of range"));
    }
    return mp_obj_new_float(s);
}
static MP_DEFINE_CONST_FUN_OBJ_KW(graphlayout_solve_obj, 5, graphlayout_solve);

static const mp_rom_map_elem_t graphlayout_module_globals_table[] = {
    { MP_ROM_QSTR(MP_QSTR___name__), MP_ROM_QSTR(MP_QSTR_graphlayout) },
    { MP_ROM_QSTR(MP_QSTR_solve), MP_ROM_PTR(&graphlayout_solve_obj) },
    { MP_ROM_QSTR(MP_QSTR_MAX_NODES), MP_ROM_INT(GRAPH_MAX_NODES) },
};
static MP_DEFINE_CONST_DICT(graphlayout_module_globals, graphlayout_module_globals_table);

const mp_obj_module_t graphlayout_user_cmodule = {
    .base = { &mp_type_module },
    .globals = (mp_obj_dict_t *)&graphlayout_module_globals,
};

MP_REGISTER_MODULE(MP_QSTR_graphlayout, graphlayout_user_cmodule);

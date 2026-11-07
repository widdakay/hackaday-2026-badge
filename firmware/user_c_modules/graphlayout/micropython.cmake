# Graph layout solver (`import graphlayout`): C++ core + MicroPython binding.
add_library(usermod_graphlayout INTERFACE)

target_sources(usermod_graphlayout INTERFACE
    ${CMAKE_CURRENT_LIST_DIR}/graph_core.cpp
    ${CMAKE_CURRENT_LIST_DIR}/modgraphlayout.c
)

target_include_directories(usermod_graphlayout INTERFACE
    ${CMAKE_CURRENT_LIST_DIR}
)

target_link_libraries(usermod INTERFACE usermod_graphlayout)

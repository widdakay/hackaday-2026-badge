# Wi-Fi FTM ranging module (`import ftm`): C++ core + MicroPython binding.
# Needs CONFIG_ESP_WIFI_FTM_ENABLE=y (boards/ESP32_GENERIC_S3/sdkconfig.ftm).
add_library(usermod_ftm INTERFACE)

target_sources(usermod_ftm INTERFACE
    ${CMAKE_CURRENT_LIST_DIR}/ftm_core.cpp
    ${CMAKE_CURRENT_LIST_DIR}/modftm.c
)

target_include_directories(usermod_ftm INTERFACE
    ${CMAKE_CURRENT_LIST_DIR}
)

target_link_libraries(usermod INTERFACE usermod_ftm)

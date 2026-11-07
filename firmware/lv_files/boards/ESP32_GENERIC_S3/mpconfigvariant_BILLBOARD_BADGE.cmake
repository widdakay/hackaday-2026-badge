set(SDKCONFIG_DEFAULTS
    ${SDKCONFIG_DEFAULTS}
    boards/sdkconfig.240mhz
    boards/sdkconfig.spiram_oct
    boards/ESP32_GENERIC_S3/sdkconfig.ftm
)

list(APPEND MICROPY_DEF_BOARD
    MICROPY_HW_BOARD_NAME="Billboard Badge Generic ESP32S3 module with Octal-SPIRAM"
)

# list(APPEND USER_C_MODULES ${CMAKE_CURRENT_LIST_DIR}/../../../../../user_c_modules/badge_utils/micropython.cmake)
list(APPEND USER_C_MODULES ${CMAKE_CURRENT_LIST_DIR}/../../../../../user_c_modules/ftm/micropython.cmake)

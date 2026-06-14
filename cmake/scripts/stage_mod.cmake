cmake_minimum_required(VERSION 3.21)

execute_process(COMMAND "${CMAKE_COMMAND}" -E make_directory "${STAGE_ROOT}/SKSE/Plugins")
execute_process(COMMAND "${CMAKE_COMMAND}" -E remove -f "${STAGE_ROOT}/SKSE/Plugins/DVCRuntime.ini")

if(DEFINED DVC_RUNTIME_DIRNAME AND NOT DVC_RUNTIME_DIRNAME STREQUAL "")
  execute_process(COMMAND "${CMAKE_COMMAND}" -E remove_directory "${STAGE_ROOT}/SKSE/Plugins/${DVC_RUNTIME_DIRNAME}")
endif()

execute_process(COMMAND "${CMAKE_COMMAND}" -E copy_if_different "${PLUGIN_FILE}" "${STAGE_ROOT}/SKSE/Plugins/${PLUGIN_NAME}")

if(DEFINED DVC_RUNTIME_APP_STAGE_ROOT AND EXISTS "${DVC_RUNTIME_APP_STAGE_ROOT}")
  execute_process(COMMAND "${CMAKE_COMMAND}" -E copy_directory
    "${DVC_RUNTIME_APP_STAGE_ROOT}"
    "${STAGE_ROOT}")
else()
  message(WARNING "Stage: runtime-app payload not found: ${DVC_RUNTIME_APP_STAGE_ROOT}")
endif()

if(EXISTS "${PAPYRUS_SRC_DIR}")
  execute_process(COMMAND "${CMAKE_COMMAND}" -E make_directory "${STAGE_ROOT}/Scripts/Source")
  execute_process(COMMAND "${CMAKE_COMMAND}" -E copy_directory "${PAPYRUS_SRC_DIR}" "${STAGE_ROOT}/Scripts/Source")
endif()

if(EXISTS "${PAPYRUS_PEX_DIR}")
  execute_process(COMMAND "${CMAKE_COMMAND}" -E make_directory "${STAGE_ROOT}/Scripts")
  execute_process(COMMAND "${CMAKE_COMMAND}" -E copy_directory "${PAPYRUS_PEX_DIR}" "${STAGE_ROOT}/Scripts")
endif()

if(EXISTS "${ESP_FILE}")
  execute_process(COMMAND "${CMAKE_COMMAND}" -E copy_if_different "${ESP_FILE}" "${STAGE_ROOT}/DragonbornVoiceControl.esp")
else()
  message(STATUS "Stage: ESP not found (optional): ${ESP_FILE}")
endif()

set(MOD_SOUND_DIR "${CMAKE_CURRENT_LIST_DIR}/../../mod/Sound")
if(EXISTS "${MOD_SOUND_DIR}")
  execute_process(COMMAND "${CMAKE_COMMAND}" -E make_directory "${STAGE_ROOT}/Sound")
  execute_process(COMMAND "${CMAKE_COMMAND}" -E copy_directory "${MOD_SOUND_DIR}" "${STAGE_ROOT}/Sound")
endif()

message(STATUS "Stage: done -> ${STAGE_ROOT}")

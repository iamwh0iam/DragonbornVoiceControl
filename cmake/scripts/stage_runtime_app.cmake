cmake_minimum_required(VERSION 3.21)

if(NOT DEFINED DVC_RUNTIME_SRC_DIR OR DVC_RUNTIME_SRC_DIR STREQUAL "")
  message(FATAL_ERROR "DVC_RUNTIME_SRC_DIR is not set")
endif()

if(NOT DEFINED DVC_RUNTIME_APP_STAGE_ROOT OR DVC_RUNTIME_APP_STAGE_ROOT STREQUAL "")
  message(FATAL_ERROR "DVC_RUNTIME_APP_STAGE_ROOT is not set")
endif()

if(NOT DEFINED DVC_RUNTIME_APP_STAGE_DIR OR DVC_RUNTIME_APP_STAGE_DIR STREQUAL "")
  message(FATAL_ERROR "DVC_RUNTIME_APP_STAGE_DIR is not set")
endif()

set(_plugins_stage_dir "${DVC_RUNTIME_APP_STAGE_ROOT}/SKSE/Plugins")

execute_process(COMMAND "${CMAKE_COMMAND}" -E make_directory "${_plugins_stage_dir}")
execute_process(COMMAND "${CMAKE_COMMAND}" -E make_directory "${DVC_RUNTIME_APP_STAGE_DIR}")

foreach(extra IN LISTS DVC_MAIN_RUNTIME_ROOT_FILES)
  if(EXISTS "${DVC_RUNTIME_SRC_DIR}/${extra}")
    execute_process(COMMAND "${CMAKE_COMMAND}" -E copy_if_different
      "${DVC_RUNTIME_SRC_DIR}/${extra}"
      "${_plugins_stage_dir}/${extra}")
  else()
    message(WARNING "runtime-app: root file not found: ${DVC_RUNTIME_SRC_DIR}/${extra}")
  endif()
endforeach()

foreach(extra IN LISTS DVC_MAIN_RUNTIME_EXTRA_FILES)
  if(EXISTS "${DVC_RUNTIME_SRC_DIR}/${extra}")
    execute_process(COMMAND "${CMAKE_COMMAND}" -E copy_if_different
      "${DVC_RUNTIME_SRC_DIR}/${extra}"
      "${DVC_RUNTIME_APP_STAGE_DIR}/${extra}")
  else()
    message(WARNING "runtime-app: extra file not found: ${DVC_RUNTIME_SRC_DIR}/${extra}")
  endif()
endforeach()

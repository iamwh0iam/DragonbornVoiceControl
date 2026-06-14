include_guard(GLOBAL)

function(dvc_add_stage_targets)
  set(runtime_app_build_root "${CMAKE_BINARY_DIR}/runtime_app")
  set(runtime_app_zip "${runtime_app_build_root}/app.zip")
  set(runtime_app_stage_root "${runtime_app_build_root}/stage")
  set(runtime_app_stage_dir "${runtime_app_stage_root}/SKSE/Plugins/${DVC_PIPE_RUNTIME_DIRNAME}")

  add_custom_target(runtime-app
    COMMAND "${CMAKE_COMMAND}" -E remove_directory "${runtime_app_stage_root}"
    COMMAND "${CMAKE_COMMAND}" -E remove -f "${runtime_app_zip}"
    COMMAND "${CMAKE_COMMAND}" -E make_directory "${runtime_app_build_root}"
    COMMAND "${CMAKE_COMMAND}" -E make_directory "${runtime_app_stage_dir}"
    COMMAND "${CMAKE_COMMAND}" -E chdir "${DVC_RUNTIME_SRC_DIR}"
      "${CMAKE_COMMAND}" -E tar cf "${runtime_app_zip}" --format=zip ${DVC_RUNTIME_APP_FILES}
    COMMAND "${CMAKE_COMMAND}" -E copy_if_different
      "${runtime_app_zip}"
      "${runtime_app_stage_dir}/app.zip"
    COMMAND "${CMAKE_COMMAND}"
      -DDVC_RUNTIME_SRC_DIR:PATH=${DVC_RUNTIME_SRC_DIR}
      -DDVC_RUNTIME_APP_STAGE_ROOT:PATH=${runtime_app_stage_root}
      -DDVC_RUNTIME_APP_STAGE_DIR:PATH=${runtime_app_stage_dir}
      "-DDVC_MAIN_RUNTIME_ROOT_FILES:STRING=${DVC_MAIN_RUNTIME_ROOT_FILES}"
      "-DDVC_MAIN_RUNTIME_EXTRA_FILES:STRING=${DVC_MAIN_RUNTIME_EXTRA_FILES}"
      -P "${CMAKE_SOURCE_DIR}/cmake/scripts/stage_runtime_app.cmake"
    VERBATIM
  )

  add_custom_target(stage-mod
    DEPENDS ${PROJECT_NAME} papyrus runtime-app
    COMMAND "${CMAKE_COMMAND}"
      -DSTAGE_ROOT:PATH=${DVC_STAGE_ROOT}
      -DPLUGIN_FILE:FILEPATH=$<TARGET_FILE:${PROJECT_NAME}>
      -DPLUGIN_NAME:STRING=$<TARGET_FILE_NAME:${PROJECT_NAME}>
      -DPAPYRUS_PEX_DIR:PATH=${DVC_PAPYRUS_PEX_DIR}
      -DPAPYRUS_SRC_DIR:PATH=${DVC_PAPYRUS_SRC_DIR}
      -DESP_FILE:FILEPATH=${DVC_ESP_FILE}
      -DDVC_RUNTIME_APP_STAGE_ROOT:PATH=${runtime_app_stage_root}
      -DDVC_RUNTIME_DIRNAME:STRING=${DVC_PIPE_RUNTIME_DIRNAME}
      -P "${CMAKE_SOURCE_DIR}/cmake/scripts/stage_mod.cmake"
    VERBATIM
  )
endfunction()

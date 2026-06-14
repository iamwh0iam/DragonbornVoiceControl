include_guard(GLOBAL)

# Optional: host python for bootloader build target
find_package(Python3 COMPONENTS Interpreter QUIET)

function(dvc_add_pyinstaller_bootloader_target)
  if(NOT WIN32)
    message(STATUS "pyinstaller-bootloader-usvfs: skipped (Windows-only)")
    return()
  endif()

  # Defaults if options were not added anywhere else
  if(NOT DEFINED DVC_PYI_BOOTLOADER_VERSION OR DVC_PYI_BOOTLOADER_VERSION STREQUAL "")
    set(DVC_PYI_BOOTLOADER_VERSION "6.19.0")
  endif()
  if(NOT DEFINED DVC_PYI_BOOTLOADER_URL OR DVC_PYI_BOOTLOADER_URL STREQUAL "")
    set(DVC_PYI_BOOTLOADER_URL "https://github.com/pyinstaller/pyinstaller/archive/refs/tags/v${DVC_PYI_BOOTLOADER_VERSION}.zip")
  endif()

  find_package(Python3 COMPONENTS Interpreter QUIET)

  add_custom_target(pyinstaller-bootloader-usvfs
    COMMAND "${CMAKE_COMMAND}"
      -DDVC_PYI_BOOTLOADER_VERSION:STRING=${DVC_PYI_BOOTLOADER_VERSION}
      -DDVC_PYI_BOOTLOADER_URL:STRING=${DVC_PYI_BOOTLOADER_URL}
      -DDVC_PYI_BOOTLOADER_FORCE_REBUILD:BOOL=$<BOOL:${DVC_PYI_BOOTLOADER_FORCE_REBUILD}>
      -DDVC_RUNTIME_BUILD_ROOT:PATH=${DVC_RUNTIME_BUILD_ROOT}
      -DDVC_HOST_PYTHON_EXE:FILEPATH=$<IF:$<BOOL:${Python3_Interpreter_FOUND}>,${Python3_EXECUTABLE},>
      -DDVC_PYI_BOOTLOADER_PATCH_FILE:FILEPATH=${CMAKE_SOURCE_DIR}/cmake/patches/pyinstaller-6.19.0-bootloader-usvfs.patch
      -DDVC_PYI_BOOTLOADER_APPLY_SCRIPT:FILEPATH=${CMAKE_SOURCE_DIR}/cmake/scripts/apply_unidiff.py
      -P "${CMAKE_SOURCE_DIR}/cmake/scripts/build_pyinstaller_bootloader.cmake"
    VERBATIM
  )
endfunction()



function(dvc_add_runtime_variant VARIANT VARIANT_LABEL REQ_FILE PYI_EXTRA_LIST)
  set(variant_dir "${DVC_RUNTIME_BUILD_ROOT}/${VARIANT}")
  set(_extra ${PYI_EXTRA_LIST})

  # app.zip modules are imported dynamically by bootstrap.py, so PyInstaller cannot
  # discover dependencies from static analysis. Add a per-variant manifest as
  # non-cache args so an existing CMake cache cannot accidentally omit it.
  if(VARIANT STREQUAL "vosk")
    list(APPEND _extra
      --hidden-import pyi_manifest_vosk
      --collect-all vosk
    )
  elseif(VARIANT STREQUAL "whisper-cpu")
    list(APPEND _extra
      --hidden-import pyi_manifest_whisper
      --collect-all vosk
    )
  elseif(VARIANT STREQUAL "whisper-gpu")
    list(APPEND _extra
      --hidden-import pyi_manifest_whisper
      --collect-all vosk
    )
  endif()

  get_filename_component(_bootstrap_name "${DVC_RUNTIME_BOOTSTRAP_ENTRY}" NAME)

  # Per-variant portable python
  set(variant_python_root "${DVC_RUNTIME_BUILD_ROOT}/_python312-${VARIANT}")
  set(variant_python_exe  "${variant_python_root}/python.exe")

  # Stage as separate mod
  set(runtime_mod_name "${DVC_RUNTIME_MOD_BASENAME} ${VARIANT_LABEL}")
  set(runtime_stage_root "${CMAKE_BINARY_DIR}/_mod/${runtime_mod_name}")

  set(_copy_runtime_extra_cmds)
  foreach(extra IN LISTS DVC_RUNTIME_EXTRA_FILES)
    list(APPEND _copy_runtime_extra_cmds
      COMMAND "${CMAKE_COMMAND}" -E copy_if_different
        "${variant_dir}/${extra}"
        "${variant_dir}/dist/${DVC_RUNTIME_NAME}/${extra}"
    )
  endforeach()

  add_custom_target("runtime-${VARIANT}"
    # 1) Prepare portable python
    COMMAND "${CMAKE_COMMAND}"
      -DDVC_PYTHON_VERSION:STRING=${DVC_PYTHON_VERSION}
      -DDVC_RUNTIME_BUILD_ROOT:PATH=${DVC_RUNTIME_BUILD_ROOT}
      -DDVC_PYTHON_ROOT:PATH=${variant_python_root}
      -DDVC_PYTHON_EXE:FILEPATH=${variant_python_exe}
      -P "${CMAKE_SOURCE_DIR}/cmake/scripts/prepare_python.cmake"

    # 2) Install requirements
    COMMAND "${CMAKE_COMMAND}"
      -DVARIANT_DIR:PATH=${variant_dir}
      -DREQ_FILE:FILEPATH=${REQ_FILE}
      -DDVC_PYTHON_EXE:FILEPATH=${variant_python_exe}
      -DDVC_PIP_UPGRADE:BOOL=$<BOOL:${DVC_PIP_UPGRADE}>
      -P "${CMAKE_SOURCE_DIR}/cmake/scripts/prepare_deps.cmake"

    # 2.5) OPTIONAL: install patched bootloader
    COMMAND "${CMAKE_COMMAND}"
      -DDVC_PYI_BOOTLOADER_VERSION:STRING=${DVC_PYI_BOOTLOADER_VERSION}
      -DDVC_RUNTIME_BUILD_ROOT:PATH=${DVC_RUNTIME_BUILD_ROOT}
      -DDVC_PYTHON_EXE:FILEPATH=${variant_python_exe}
      -DDVC_PYTHON_ROOT:PATH=${variant_python_root}
      -DDVC_PYI_BOOTLOADER_FORCE_INSTALL:BOOL=$<BOOL:${DVC_PYI_BOOTLOADER_FORCE_INSTALL}>
      -P "${CMAKE_SOURCE_DIR}/cmake/scripts/install_pyinstaller_bootloader_if_present.cmake"

    # 3) Copy runtime sources into variant_dir and bake this runtime variant into the bootstrap.
    COMMAND "${CMAKE_COMMAND}" -E make_directory "${variant_dir}"
    COMMAND "${CMAKE_COMMAND}" -E copy_directory "${DVC_RUNTIME_SRC_DIR}" "${variant_dir}"
    COMMAND "${CMAKE_COMMAND}"
      -DDVC_RUNTIME_VARIANT:STRING=${VARIANT}
      -DDVC_RUNTIME_VARIANT_FILE:FILEPATH=${variant_dir}/runtime_variant.py
      -P "${CMAKE_SOURCE_DIR}/cmake/scripts/write_runtime_variant.cmake"

    # 4) Run PyInstaller on bootstrap.py only.
    # Actual Python application code is supplied by main-mod app.zip at runtime.
    COMMAND "${CMAKE_COMMAND}" -E chdir "${variant_dir}"
      "${variant_python_exe}" -m PyInstaller
        --noconfirm
        --clean
        --onedir
        --name "${DVC_RUNTIME_NAME}"
        --icon "dvcs2.ico"
        ${_extra}
        "${_bootstrap_name}"

    ${_copy_runtime_extra_cmds}

    VERBATIM
  )

  # 5) Stage runtime mod: only the PyInstaller runtime folder, no INI/app.zip/json.
  add_custom_command(TARGET "runtime-${VARIANT}" POST_BUILD
    COMMAND "${CMAKE_COMMAND}" -E remove_directory
      "${runtime_stage_root}/SKSE/Plugins/${DVC_PIPE_RUNTIME_DIRNAME}"
    COMMAND "${CMAKE_COMMAND}" -E make_directory
      "${runtime_stage_root}/SKSE/Plugins/${DVC_PIPE_RUNTIME_DIRNAME}"
    COMMAND "${CMAKE_COMMAND}" -E copy_directory
      "${variant_dir}/dist/${DVC_RUNTIME_NAME}"
      "${runtime_stage_root}/SKSE/Plugins/${DVC_PIPE_RUNTIME_DIRNAME}"
    VERBATIM
  )
endfunction()

function(dvc_add_runtime_targets)
  # Add optional bootloader build target
  dvc_add_pyinstaller_bootloader_target()

  if(DVC_BUILD_VOSK)
    dvc_add_runtime_variant("vosk" "Vosk" "${DVC_REQ_VOSK}" "${DVC_PYI_EXTRA_VOSK}")
  endif()
  if(DVC_BUILD_WHISPER_CPU)
    dvc_add_runtime_variant("whisper-cpu" "WhisperCPU" "${DVC_REQ_WHISPER_CPU}" "${DVC_PYI_EXTRA_WHISPER_CPU}")
  endif()
  if(DVC_BUILD_WHISPER_GPU)
    dvc_add_runtime_variant("whisper-gpu" "WhisperGPU" "${DVC_REQ_WHISPER_GPU}" "${DVC_PYI_EXTRA_WHISPER_GPU}")
  endif()
endfunction()

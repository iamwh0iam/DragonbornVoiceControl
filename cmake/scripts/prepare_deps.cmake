cmake_minimum_required(VERSION 3.21)

if(NOT EXISTS "${DVC_PYTHON_EXE}")
  message(FATAL_ERROR "Python not found: ${DVC_PYTHON_EXE}")
endif()

file(MAKE_DIRECTORY "${VARIANT_DIR}")

set(_stamp "${VARIANT_DIR}/.stamp_deps")

if(EXISTS "${_stamp}")
  message(STATUS "Deps: already prepared -> ${VARIANT_DIR}")
  return()
endif()

if(DVC_PIP_UPGRADE)
  message(STATUS "Deps: upgrading pip/setuptools/wheel")
  execute_process(
    COMMAND "${DVC_PYTHON_EXE}" -m pip install --upgrade pip setuptools wheel
            --disable-pip-version-check --no-warn-script-location --no-user
    RESULT_VARIABLE _rv0
  )
  if(NOT _rv0 EQUAL 0)
    message(FATAL_ERROR "pip upgrade failed (rv=${_rv0}).")
  endif()
else()
  message(STATUS "Deps: ensuring setuptools/wheel")
  execute_process(
    COMMAND "${DVC_PYTHON_EXE}" -m pip install setuptools wheel
            --disable-pip-version-check --no-warn-script-location --no-user
    RESULT_VARIABLE _rv0
  )
  if(NOT _rv0 EQUAL 0)
    message(FATAL_ERROR "pip install setuptools/wheel failed (rv=${_rv0}).")
  endif()
endif()

if(EXISTS "${REQ_FILE}")
  message(STATUS "Deps: installing requirements into portable python (no --target)")
  execute_process(
    COMMAND "${DVC_PYTHON_EXE}" -m pip install --no-build-isolation -r "${REQ_FILE}"
            --disable-pip-version-check --no-warn-script-location --no-user
    RESULT_VARIABLE _rv1
  )
  if(NOT _rv1 EQUAL 0)
    message(FATAL_ERROR "pip install -r failed (rv=${_rv1}).")
  endif()
else()
  message(FATAL_ERROR "Requirements file not found: ${REQ_FILE}")
endif()

file(WRITE "${_stamp}" "ok")
message(STATUS "Deps: OK (installed into python) -> ${DVC_PYTHON_EXE}")

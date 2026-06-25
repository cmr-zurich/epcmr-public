#-----------------------------------------------------------------------------
# Extension metadata
#-----------------------------------------------------------------------------
set(EXTENSION_NAME EPCMR)
set(EXTENSION_HOMEPAGE "https://github.com/cmr-zurich/epcmr")
set(EXTENSION_CATEGORY "Cardiac")
set(EXTENSION_CONTRIBUTORS "Ingo Paetsch (Heart Center Leipzig), Max Fuetterer (ETH Zurich)")
set(EXTENSION_DESCRIPTION "Multi-procedure EP-CMR suite for RA Flutter and PVC Ablation.")
set(EXTENSION_ICON "Resources/Icons/EPCMR.png")
set(EXTENSION_SCREENSHOT "")

#-----------------------------------------------------------------------------
# Module name
#-----------------------------------------------------------------------------
set(MODULE_NAME EPCMR)

#-----------------------------------------------------------------------------
# Python scripts
#-----------------------------------------------------------------------------
set(MODULE_PYTHON_SCRIPTS
  ${MODULE_NAME}.py
)

#-----------------------------------------------------------------------------
# Resources
#-----------------------------------------------------------------------------
set(MODULE_PYTHON_RESOURCES
  Resources/Icons/EPCMR.png
  Resources/UI/RAFlutter.ui
)

#-----------------------------------------------------------------------------
# Build the scripted module
#-----------------------------------------------------------------------------
slicerMacroBuildScriptedModule(
  NAME ${MODULE_NAME}
  SCRIPTS ${MODULE_PYTHON_SCRIPTS}
  RESOURCES ${MODULE_PYTHON_RESOURCES}
  WITH_GENERIC_TESTS
)

#-----------------------------------------------------------------------------
# Testing
#-----------------------------------------------------------------------------
if(BUILD_TESTING)
  slicer_add_python_unittest(SCRIPT ${MODULE_NAME}.py)
  add_subdirectory(Testing)
endif()

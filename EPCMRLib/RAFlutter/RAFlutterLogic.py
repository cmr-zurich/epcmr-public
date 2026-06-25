# ruff: isort: skip_file

# ---- Standard Python (Built-ins) ----
import logging

# ---- Core "V-Q-C-S" Stack (Mnemonic: Visualize Quickly Configure Slicer) ----
import vtk
import qt
import ctk
import slicer

# ---- Slicer Specialized Classes ----
from slicer.ScriptedLoadableModule import *


class RAFlutterLogic(ScriptedLoadableModuleLogic):
    """
    RAFlutterLogic handles the MRML manipulation and algorithm execution
    for Right Atrial Flutter procedures.
    """

    def __init__(self):
        # Initialize the base class
        ScriptedLoadableModuleLogic.__init__(self)
        self.mappingPtsNode = None

    # ----------------------------------------------------------------------
    # Mapping Points -- Creation & Scene-Safe Retrieval
    # ----------------------------------------------------------------------

    def getOrCreateMappingPoints(self):
        """
        Ensures a markups node exists for mapping points.
        Returns the vtkMRMLMarkupsFiducialNode.
        """
        node_name = "mappingPts"

        # Always check the scene first to avoid stale references after a Scene Close
        self.mappingPtsNode = slicer.mrmlScene.GetFirstNodeByName(node_name)

        if not self.mappingPtsNode:
            # Use the newer AddNewNodeByClass for Slicer 5.x
            self.mappingPtsNode = slicer.mrmlScene.AddNewNodeByClass("vtkMRMLMarkupsFiducialNode", node_name)

        return self.mappingPtsNode

    # ----------------------------------------------------------------------
    # RA Flutter -- Interpolation Pipeline Entry Point
    # ----------------------------------------------------------------------

    def run_process(self):
        """
        Executes the specific RA Flutter setup logic.

        Priority:
          1) If the global EPCMR setup utility exists, delegate to it.
          2) Otherwise, trigger the new interpolation pipeline via the
             REAL EPCMRLogic instance stored on the Python module `EPCMR`,
             not the auto-created Slicer stub logic.

        Notes:
          - slicer.modules.epcmr.logic() returns a stub vtkSlicerScriptedLoadableModuleLogic
            which has no SceneManager, no ModelObserver, and no wrapped parameter node.
          - The REAL EPCMRLogic instance is created in EPCMRWidget.setup() and registered
            globally via logic.registerAsGlobalLogic(), stored as EPCMR._realLogic.
          - Accessing EPCMR._realLogic is the only correct way to reach the workflow engine.
        """

        # 1) Preferred path: global EPCMR setup helper
        if hasattr(slicer.util, "runEPCMRsetup"):
            slicer.util.runEPCMRsetup()
            return True

        # 2) Fallback: manually trigger the new interpolation pipeline
        try:
            # ------------------------------------------------------------------
            # Always use the REAL EPCMRLogic instance (not the Slicer stub)
            # ------------------------------------------------------------------
            import EPCMR

            mainLogic = getattr(EPCMR, "_realLogic", None)

            if not mainLogic:
                logging.error("RAFlutterLogic: REAL EPCMRLogic instance not found (EPCMR._realLogic missing).")
                return False

            # Ensure mappingPts node exists in the scene
            pts = self.getOrCreateMappingPoints()

            # Keep EPCMRParameterNode wrapper in sync, if available
            if hasattr(mainLogic, "getParameterNode"):
                try:
                    pNode = mainLogic.getParameterNode()
                except Exception:
                    pNode = None
                if pNode is not None and hasattr(pNode, "mappingPts"):
                    pNode.mappingPts = pts

            # Ensure ModelObserver facade exists
            modelObserver = getattr(mainLogic, "modelObserver", None)
            if not modelObserver:
                logging.error("RAFlutterLogic: mainLogic.modelObserver is not available.")
                return False

            # Wire mappingPts into MappingEventController via facade
            if hasattr(modelObserver, "setAndObserveMappingNode"):
                modelObserver.setAndObserveMappingNode(pts)

            # Trigger Shepard-based interpolation via GeometryInterpolator
            if hasattr(modelObserver, "interpolator") and hasattr(modelObserver.interpolator, "run"):
                modelObserver.interpolator.run(pts)
                return True

            logging.error(
                "RAFlutterLogic: ModelObserver interpolator is missing or incomplete "
                "(expected modelObserver.interpolator.run(markupsNode))."
            )
            return False

        except Exception as e:
            logging.error(f"RAFlutterLogic: run_process failed: {e}")
            return False

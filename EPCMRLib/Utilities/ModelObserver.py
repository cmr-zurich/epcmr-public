# ruff: isort: skip_file
import os
import slicer

from EPCMRLib.Utilities.ColorMapper import ColorMapper
from EPCMRLib.Utilities.RACloneManager import RACloneManager
from EPCMRLib.Utilities.GeometryInterpolator import GeometryInterpolator
from EPCMRLib.Utilities.MappingEventController import MappingEventController


class ModelObserver:
    """
    Unified facade for all mapping-related subsystems.

    Coordinates/delegates:
      - RACloneManager        -> geometry and visibility (RA + cloned RA)
      - ColorMapper           -> colormaps (activation + voltage LUTs)
      - GeometryInterpolator  -> interpolation (Activation Time + Voltage interpolation)
      - MappingEventController-> single deterministic dispatcher for MRML events
      - SceneManager          -> legends + lighting (incl. shaders)

    This class owns the entire mapping pipeline and exposes a clean,
    stable API to EPCMRLogic.
    """

    def __init__(self, pNode, sceneManager, savePath=None):
        """
        pNode:
            EPCMRParameterNode wrapper (workflow-level API).
        sceneManager:
            SceneManager instance that owns activation LUT, voltage LUT,
            scalar bars, and all color/legend infrastructure.
        savePath:
            Optional study data path; defaults to a user home subfolder.
        """
        if type(pNode).__name__ != "EPCMRParameterNode":
            raise TypeError("ModelObserver requires EPCMRParameterNode wrapper")

        self.pNode = pNode
        self.sceneManager = sceneManager

        # ------------------------------------------------------------------
        # Subsystems
        # ------------------------------------------------------------------

        # RA geometry + cloned RA lifecycle
        # Deterministic RA/clone resolution is handled entirely by RACloneManager.
        self.raManager = RACloneManager(self.pNode)

        # Activation LUT comes from SceneManager (Sascha's Rainbow)
        activation_color_node = getattr(self.sceneManager, "activationColorNode", None)
        self.colorMapper = ColorMapper(activation_color_node)

        # Geometry interpolation (Activation Time + Voltage)
        # IMPORTANT:
        #   GeometryInterpolator now requires FOUR arguments:
        #       (pNode, ra_clone_manager, color_mapper, sceneManager)
        #
        #   This ensures:
        #       - RA + clone always resolved via RACloneManager
        #       - Activation LUT + Voltage LUT + legends come from SceneManager
        #       - No scene lookups inside GeometryInterpolator
        #
        self.interpolator = GeometryInterpolator(
            self.pNode,
            self.raManager,
            self.colorMapper,
            self.sceneManager,
        )

        # Centralized mapping event controller
        # (single deterministic dispatcher for all markups events)
        self.mappingController = MappingEventController(
            self.pNode,
            self.raManager,
            self.interpolator,
            self.sceneManager,
        )

        # ------------------------------------------------------------------
        # Save path
        # ------------------------------------------------------------------
        self.savePath = savePath or os.path.join(os.path.expanduser("~"), "SlicerEPCMRStudyData")
        os.makedirs(self.savePath, exist_ok=True)

        # ------------------------------------------------------------------
        # Initial RA display (neutral state, no mapping points)
        # ------------------------------------------------------------------
        if self.raManager.RA:
            self.raManager.set_neutral_state(num_points=0)

    # ----------------------------------------------------------------------
    # Public API used by EPCMRLogic
    # ----------------------------------------------------------------------
    def setAndObserveMappingNode(self, markupsNode):
        """
        Attach MRML observers to the mappingPts node and immediately
        update RA / cloned RA display state based on current point count.

        This is called once from EPCMRLogic.setupSceneManager() after
        mappingPts has been created or recovered.

        Behavior:
          - Wires observers via MappingEventController
          - Immediately applies correct RA state:
              - 0 points -> neutral state
              - >=1 points -> MappingEventController decides correct state
        """
        if not markupsNode:
            return

        # Wire MRML observers (PointAdded/Removed/Modified)
        self.mappingController.set_mapping_node(markupsNode)

        # Apply initial state based on existing points
        n = markupsNode.GetNumberOfControlPoints()
        if n == 0:
            # Neutral RA (no mapping)
            self.raManager.set_neutral_state(num_points=0)
        else:
            # Let the controller derive the correct state
            self.mappingController.on_point_modified(markupsNode)

    # ----------------------------------------------------------------------
    # Optional cleanup hook
    # ----------------------------------------------------------------------
    def cleanup(self):
        """
        Release references so that external owners (EPCMRLogic) can
        safely drop this observer without leaking MRML observers.
        """
        # Detach MRML observers
        if self.mappingController:
            self.mappingController.cleanupObservers()

        # Drop references
        self.mappingController = None
        self.interpolator = None
        self.colorMapper = None
        self.raManager = None
        self.sceneManager = None
        self.pNode = None

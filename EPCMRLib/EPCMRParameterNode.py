import slicer
from slicer.parameterNodeWrapper import parameterNodeWrapper


@parameterNodeWrapper
class EPCMRParameterNode:
    """
    Workflow-level state for EPCMR.
    Shared by SceneManager, ModelObserver, Widgets, Replayer, and now Free Angulator.

    Notes:
      - MRML node references are pointers only (not owned/serialized).
      - Primitive values persist across sessions.
      - Mapping mode selects Shepard-kernel interpolation strategy.
      - Free Angulator integration stores slice geometry + active free slice.
    """

    # ------------------------------------------------------------------
    # MRML NODE REFERENCES (workflow-level pointers, not owned)
    # ------------------------------------------------------------------
    ablModel: slicer.vtkMRMLModelNode
    refModel: slicer.vtkMRMLModelNode
    ablTransform: slicer.vtkMRMLLinearTransformNode
    refTransform: slicer.vtkMRMLLinearTransformNode
    ablReplayTransform: slicer.vtkMRMLLinearTransformNode
    refReplayTransform: slicer.vtkMRMLLinearTransformNode
    mappingPts: slicer.vtkMRMLMarkupsFiducialNode
    ablationPts: slicer.vtkMRMLMarkupsFiducialNode
    raModel: slicer.vtkMRMLModelNode
    raClonedModel: slicer.vtkMRMLModelNode
    rvModel: slicer.vtkMRMLModelNode

    # ------------------------------------------------------------------
    # PRIMITIVE STATE (LIVE + REPLAY)
    # ------------------------------------------------------------------

    # LIVE-mode debounce timestamps (seconds since epoch)
    lastAblationUpdateTime: float = 0.0
    lastReferenceUpdateTime: float = 0.0

    # replayModeActive:
    # True when the USER has switched the module into REPLAY mode.
    replayModeActive: bool = False

    # replayerActive:
    # True only while the REPLAY ENGINE is actively driving transforms/tinting.
    replayerActive: bool = False

    # Replay metadata + runtime state
    replayFilePath: str = ""
    currentReplayFrame: int = 0
    totalReplayFrames: int = 0
    replaySpeed: float = 1.0
    activeWorkflowIndex: int = 0
    isReplayerRunning: bool = False
    lastSavePath: str = ""

    # Per-frame catheter validity table (index -> {"Abl_01": bool, "Ref_01": bool})
    validityTable = {}

    # ------------------------------------------------------------------
    # MAPPING CONFIGURATION
    # ------------------------------------------------------------------
    mappingMode: str = "Activation Time Mapping"
    mappingPhase: str = "POST"

    # CARTO-style voltage smoothing parameters
    cartoDistanceThresholdMm: float = 7.0
    cartoGaussianSharpness: float = 3.0

    # True while a mapping mode switch is in progress.
    modeSwitchInProgress: bool = False

    # ------------------------------------------------------------------
    # SERIALIZER-SAFE COLOR CONFIGURATION
    # ------------------------------------------------------------------
    invalidRedTint = [1.0, 0.85, 0.85]
    invalidGreenTint = [0.85, 1.0, 0.85]

    # ------------------------------------------------------------------
    # FREE ANGULATOR INTEGRATION (NEW)
    # ------------------------------------------------------------------
    # Name of the currently free slice ("Red", "Green", "Yellow", or "")
    freeAngulatorActiveSlice: str = ""

    # List of stored geometry names (e.g. ["Target1", "Baseline", ...])
    # Stored as a Python list for serializer compatibility.
    freeAngulatorStoredNames = []

    # Dictionary mapping:
    #   "Name_Red"    -> serialized 4x4 matrix string
    #   "Name_Green"  -> serialized 4x4 matrix string
    #   "Name_Yellow" -> serialized 4x4 matrix string
    #
    # Example:
    #   freeAngulatorGeometry["Baseline_Red"] = "1 0 0 0  0 1 0 0  ..."
    #
    # This allows FreeAngulatorLogic to store/restore geometry
    # WITHOUT needing its own MRML node.
    freeAngulatorGeometry = {}

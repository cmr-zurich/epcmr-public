# ruff: isort: skip_file

# ---- 1. Standard Slicer/Python Imports ----
import os
import sys
import time
import logging
import subprocess

import vtk
import qt
import ctk
import slicer
from slicer.ScriptedLoadableModule import *


# ---- 2. Path Injection (MUST BE FIRST) ----
# Excellent path injection code: This script automatically handles its own
# subfolder structure by dynamically locating the current directory. It appends
# the internal "EPCMRLib" to the Python search path (sys.path), completely
# eliminating import path complexity across different development environments.
moduleDir = os.path.dirname(os.path.abspath(__file__))
if moduleDir not in sys.path:
    sys.path.append(moduleDir)
    libDir = os.path.join(moduleDir, "EPCMRLib")
    if libDir not in sys.path:
        sys.path.append(libDir)

# ---- 3. Internal Imports ----
from EPCMRLib.Utilities.SceneManager import SceneManager  # noqa: E402
from EPCMRLib.EPCMRParameterNode import EPCMRParameterNode  # noqa: E402
from EPCMRLib.FreeAngulator.FreeAngulatorWidget import FreeAngulatorWidget  # noqa: E402


def ensure_pyigtl_installed() -> bool:
    """
    Ensure that the Python package 'pyigtl' is available in Slicer's embedded
    Python environment.

    This helper is safe to call at module startup (via a QTimer) and works in
    Slicer 5.7+. It attempts to import 'pyigtl' first; if the import fails, it
    installs the package using Slicer's own Python executable ('sys.executable')
    to guarantee installation into the correct environment. User-visible status
    messages are shown via 'slicer.util.infoDisplay' and 'errorDisplay'.

    Returns:
        True  - if 'pyigtl' is already installed or installation succeeds.
        False - if installation fails.
    """
    try:
        import pyigtl

        print("pyigtl is installed !")
        return True
    except ImportError:
        pass

    python_exe = sys.executable
    cmd = [python_exe, "-m", "pip", "install", "pyigtl"]

    try:
        slicer.util.infoDisplay("EPCMR: Installing required Python package 'pyigtl'...")
        subprocess.check_call(cmd)
        slicer.util.infoDisplay("EPCMR: 'pyigtl' installed successfully.")
        return True
    except Exception as e:
        slicer.util.errorDisplay(f"EPCMR: Failed to install 'pyigtl': {e}")
        return False


# --------------------------------------------------------------------------
# 1. NOTE: MUST be defined FIRST !!! EPCMR Main Logic Class (The Engine)
# --------------------------------------------------------------------------
"""
EPCMRLogic maintains two distinct parameter-node attributes:

1. self._parameterNode
   --------------------
   - This is the *raw MRML node* (vtkMRMLScriptedModuleNode).
   - It is created or retrieved by GetParameterNode().
   - It lives in the MRML scene and fires MRML events.
   - Slicer uses it for:
       - observers
       - scene membership
       - serialization (save/load)
       - default parameter initialization

2. self._parameterNodeWrapped
   ---------------------------
   - This is the *wrapped* EPCMRParameterNode.
   - It is created like this:
         self._parameterNodeWrapped = EPCMRParameterNode(self._parameterNode)
   - It provides typed workflow attributes such as:
         ablationPts, mappingPts, ablModel, refModel,
         transforms, lastSavePath, invalidRedTint, etc.
   - It is used by:
         - SceneManager
         - ModelObserver
         - RAFlutterWidget / PVCWidget
         - CatheterReplayer
         - All EPCMR workflow logic

Summary:
   self._parameterNode          -> raw MRML node (Slicer-level)
   self._parameterNodeWrapped   -> wrapped EPCMRParameterNode (workflow-level)
"""


class EPCMRLogic(ScriptedLoadableModuleLogic):
    def __init__(self):
        super().__init__()

        # IGTL connector creation retry state (Slicer 5.10 timing guard)
        self._igtlRetryCount = 0
        self._igtlMaxRetries = 20  # 20 * 250 ms = 5 seconds max wait

        # RAW MRML parameter node (vtkMRMLScriptedModuleNode)
        self._parameterNode = None

        # WRAPPED EPCMRParameterNode (Python workflow API)
        self._parameterNodeWrapped = None

        # SceneManager (receives wrapped node)
        self.sceneManager = None

        # ModelObserver (receives wrapped node)
        self.modelObserver = None

        # Scene / parameter observers
        self._pNodeObserver = None
        self.sceneObserverTag = None
        self.sceneEndCloseObserverTag = None

        # Will be used to detect when the scene is fully initialized
        # (vtkMRMLScene.EndImportEvent) so IGTL nodes can be created safely.
        self.sceneEndImportObserverTag = None

        # Shortcut / replayer / timers
        self.shortcutManager = None
        self._replayerInstance = None
        self.syncTimer = None

        self._cachedTipPos = None
        self._cachedTipValid = False

    def _ensureIgtlConnector(self):
        """
        Ensure that the IGTL connector exists and is started.
        Modern Slicer versions (5.7+) allow safe probing by attempting to
        create a temporary vtkMRMLIGTLConnectorNode.
        """

        app = slicer.app
        mm = app.moduleManager()
        igtl_module = mm.module("OpenIGTLinkIF")

        if igtl_module is None or not hasattr(slicer.modules, "openigtlinkif"):
            logging.warning("EPCMR: OpenIGTLinkIF module is not available. Skipping IGTL connector creation.")
            return

        # Probe: try to create a temporary IGTL connector node.
        probe = slicer.mrmlScene.AddNewNodeByClass("vtkMRMLIGTLConnectorNode", "IGTL_PROBE_EPCMR")
        if probe is None:
            logging.error(
                "EPCMR: OpenIGTLinkIF is loaded, but vtkMRMLIGTLConnectorNode "
                "is not registered yet. Cannot create IGTL connector node."
            )
            return
        else:
            slicer.mrmlScene.RemoveNode(probe)

        # Safe to create or retrieve the actual EPCMR connector node.
        cnode = slicer.mrmlScene.GetFirstNodeByName("IGTLConnector_EPCMR")
        if not cnode:
            cnode = slicer.mrmlScene.AddNewNodeByClass("vtkMRMLIGTLConnectorNode", "IGTLConnector_EPCMR")

        if cnode is None:
            logging.error(
                "EPCMR: Failed to create IGTL connector node even though vtkMRMLIGTLConnectorNode is creatable in this scene."
            )
            return

        cnode.SetTypeClient("localhost", 18944)
        if cnode.GetState() == slicer.vtkMRMLIGTLConnectorNode.StateOff:
            cnode.Start()
            logging.info("EPCMR: IGTL connector started (localhost:18944).")

    def getReplayer(self):
        """
        Lazy factory for CatheterReplayer.
        Creates the replayer only when first requested.
        Ensures SceneManager has already loaded models and transforms.
        """
        if self._replayerInstance is not None:
            return self._replayerInstance

        try:
            # CORRECT IMPORT PATH
            from EPCMRLib.Utilities.CatheterReplayer import CatheterReplayer

            pNodeWrapped = self.getParameterNode()
            self._replayerInstance = CatheterReplayer(pNodeWrapped, sceneManager=self.sceneManager)
            logging.info("EPCMRLogic: CatheterReplayer created lazily via getReplayer().")
            return self._replayerInstance

        except Exception as e:
            logging.error(f"EPCMRLogic: Failed to create CatheterReplayer: {e}")
            self._replayerInstance = None
            return None

    # ------------------------------------------------------------------
    # 1. Raw MRML node creation (Slicer API)
    # ------------------------------------------------------------------
    def SetDefaultParameters(self, parameterNode):
        """
        Initialize default parameter values on first creation of the parameter node.
        This method is called automatically by GetParameterNode() when the node
        is newly created or recovered after scene reload.
        """
        if not parameterNode.GetParameter("initialized"):
            parameterNode.SetParameter("initialized", "true")
            parameterNode.SetParameter("authoritativeReplayIndex", "0")
            parameterNode.SetParameter("lastSavePath", "")
            parameterNode.Modified()

    def GetParameterNode(self):
        """
        Returns the RAW MRML parameter node (vtkMRMLScriptedModuleNode).
        Never override or shadow this method.
        """
        if not self._parameterNode or self._parameterNode.GetScene() != slicer.mrmlScene:
            node = slicer.mrmlScene.GetSingletonNode("EPCMR_WorkflowState", "vtkMRMLScriptedModuleNode")
            if not node:
                node = slicer.mrmlScene.AddNewNodeByClass("vtkMRMLScriptedModuleNode", "EPCMR_WorkflowState")
            node.SetName("EPCMR_WorkflowState")
            self._parameterNode = node
            self.SetDefaultParameters(self._parameterNode)

        return self._parameterNode

    # ------------------------------------------------------------------
    # 2. Wrapped node creation (EPCMR API)
    # ------------------------------------------------------------------
    def getParameterNode(self):
        """
        Returns the WRAPPED EPCMRParameterNode.
        This is the node used by SceneManager, ModelObserver, Widgets, Replayer.
        """
        if self._parameterNodeWrapped is None:
            raw = self.GetParameterNode()
            self._parameterNodeWrapped = EPCMRParameterNode(raw)
        return self._parameterNodeWrapped

    # ------------------------------------------------------------------
    # 3. SceneManager / ModelObserver setup
    # ------------------------------------------------------------------
    def setupSceneManager(self) -> None:
        """
        Initialize all EPCMR scene-level managers and observers.
        """
        scene = slicer.mrmlScene

        # Ensure raw + wrapped nodes exist
        self.GetParameterNode()
        pNodeWrapped = self.getParameterNode()

        # Create SceneManager if not yet created
        if self.sceneManager is None:
            self.sceneManager = SceneManager(pNodeWrapped)

        self.sceneManager.initializePaths()

        from EPCMRLib.Utilities.ModelObserver import ModelObserver

        savePath = pNodeWrapped.lastSavePath
        self.modelObserver = ModelObserver(
            pNodeWrapped,
            self.sceneManager,
            savePath,
        )

        mappingPts = getattr(pNodeWrapped, "mappingPts", None)
        if mappingPts is not None:
            self.modelObserver.setAndObserveMappingNode(mappingPts)

        self.shortcutManager = None
        self._replayerInstance = None

        self.syncTimer = qt.QTimer()
        self.syncTimer.setSingleShot(True)
        self.syncTimer.timeout.connect(self.onSyncTimerTimeout)

        # Dedicated Scene Observer
        if self.sceneObserverTag is None:
            self.sceneObserverTag = scene.AddObserver(
                slicer.vtkMRMLScene.NodeAddedEvent,
                self.onNodeAdded,
            )

        # Scene reset observers (Ctrl+W, Load Scene)
        if self.sceneEndCloseObserverTag is None:
            self.sceneEndCloseObserverTag = scene.AddObserver(
                slicer.vtkMRMLScene.EndCloseEvent,
                self.onSceneClosed,
            )

        if self.sceneEndImportObserverTag is None:
            self.sceneEndImportObserverTag = scene.AddObserver(
                slicer.vtkMRMLScene.EndImportEvent,
                self.onSceneClosed,
            )

        self.addParameterNodeObservers()

    def addParameterNodeObservers(self):
        """
        Attach observers to the parameter node.
        Ensures EPCMRLogic reacts to parameter changes and survives UI reloads.
        """
        pNode = self.GetParameterNode()

        # Remove old observer if it exists
        if hasattr(self, "_pNodeObserver") and self._pNodeObserver is not None:
            try:
                pNode.RemoveObserver(self._pNodeObserver)
            except Exception:
                pass

        # Attach new observer
        self._pNodeObserver = pNode.AddObserver(vtk.vtkCommand.ModifiedEvent, self.onParameterNodeModified)

    def onParameterNodeModified(self, caller, event):
        """
        Callback for parameter node changes.
        Extend this method to react to workflow state updates
        (e.g., replay index changes, save path updates, etc.).
        """
        # Currently no-op; kept for future workflow state reactions.
        pass

    # ------------------------------------------------------------------
    # 5. Clinical scene setup
    # ------------------------------------------------------------------
    def setupEPCMRScene(self):
        """
        Clinical setup logic: Delegates loading and visual markers to SceneManager.

        IMPORTANT:
        ----------
        mappingPts and ablationPts are CREATED HERE.
        Therefore markups observers MUST be attached *after* this method finishes.

        This guarantees that SceneManager.setupMarkupsObservers() receives valid
        markups nodes and can populate _markupObserverTags correctly.
        """
        # 1. Resolve Resource Paths
        try:
            modulePath = slicer.modules.epcmr.path
            resourcesPath = os.path.join(os.path.dirname(modulePath), "Resources")
        except AttributeError:
            modulePath = slicer.modules.epcmr.path
            resourcesPath = os.path.join(os.path.dirname(modulePath), "Resources")

        if not os.path.exists(resourcesPath):
            logging.error(f"EPCMR: CRITICAL - Resources folder NOT FOUND at {resourcesPath}")
        else:
            logging.info(f"EPCMR: Resources path resolved: {resourcesPath}")

        # Ensure wrapped node exists
        pNode = self.getParameterNode()

        # Enforce a default for mappingPhase to guarantee consistency if older
        # scenes are loaded
        if not getattr(pNode, "mappingPhase", None):
            pNode.mappingPhase = "POST"

        # Setup Mapping Points
        mappingNode = slicer.util.getFirstNodeByName("mappingPts")
        if not mappingNode:
            mappingNode = slicer.mrmlScene.AddNewNodeByClass("vtkMRMLMarkupsFiducialNode", "mappingPts")
            mappingNode.CreateDefaultDisplayNodes()
            if mappingNode.GetDisplayNode():
                mappingNode.GetDisplayNode().SetGlyphScale(1.5)
                mappingNode.GetDisplayNode().SetSelectedColor(1, 1, 0)
        pNode.mappingPts = mappingNode

        # Wire mappingPts to ModelObserver if available
        if hasattr(self, "modelObserver") and self.modelObserver:
            try:
                self.modelObserver.setAndObserveMappingNode(mappingNode)
            except Exception as e:
                logging.error(f"EPCMR: Failed to wire mappingPts to ModelObserver: {e}")

        # Setup Ablation Points
        ablationNode = slicer.util.getFirstNodeByName("ablationPts")
        if not ablationNode:
            ablationNode = slicer.mrmlScene.AddNewNodeByClass("vtkMRMLMarkupsFiducialNode", "ablationPts")
            ablationNode.CreateDefaultDisplayNodes()
            if ablationNode.GetDisplayNode():
                ablationNode.GetDisplayNode().SetGlyphScale(2.0)
                ablationNode.GetDisplayNode().SetSelectedColor(1, 0, 0)
        pNode.ablationPts = ablationNode

        # ShortcutManager
        if self.shortcutManager is None:
            try:
                from EPCMRLib.Utilities.ShortcutManager import ShortcutManager

                mw = slicer.util.mainWindow()
                self.shortcutManager = ShortcutManager(mw, pNode)
                logging.info("EPCMR: ShortcutManager initialized and bound to Main Window.")
            except Exception as e:
                logging.error(f"EPCMR: Failed to initialize ShortcutManager: {e}")

        if self.shortcutManager:
            self.shortcutManager.syncParameterNode()

        # Delegate clinical model loading to SceneManager
        if self.sceneManager:
            if hasattr(self.sceneManager, "loadClinicalModels"):
                self.sceneManager.loadClinicalModels(resourcesPath, transformCallback=self.onMyTransformModified)
                # --- Restore automatic anatomy coloring ---
                for modelNode in slicer.util.getNodesByClass("vtkMRMLModelNode"):
                    try:
                        self.sceneManager.autoColorAnatomy(modelNode)
                    except Exception as e:
                        logging.error(f"Auto-coloring failed for {modelNode.GetName()}: {e}")

                # Re-bind replayer models after SceneManager loads them
                try:
                    w = slicer.modules.epcmr.widgetRepresentation()
                    if hasattr(w.self(), "replayer") and w.self().replayer:
                        w.self().replayer.setup_nodes()
                        logging.info("Replayer nodes re-bound after model load.")
                except Exception as e:
                    logging.error(f"Failed to re-bind replayer nodes: {e}")

            # ------------------------------------------------------------------
            # Resolve the ORIGINAL anatomical RA model.
            #
            # IMPORTANT:
            #   The anatomical RA model's name MUST start with "RightAtriumCardiac"
            #   (e.g. "RightAtriumCardiac BJ 78465309", "RightAtriumCardiac_001", etc.)
            #
            #   This prefix-based rule ensures deterministic selection across
            #   all patient-specific datasets and avoids accidental selection
            #   of clones or unrelated models.
            # ------------------------------------------------------------------
            raModel = None

            for m in slicer.util.getNodesByClass("vtkMRMLModelNode"):
                name = m.GetName() or ""
                if name.startswith("RightAtriumCardiac") and "Cloned" not in name:
                    raModel = m
                    break

            if raModel:
                pNode.raModel = raModel

            if raModel:
                pNode.raModel = raModel

            if hasattr(self.sceneManager, "setupCustomOrientationMarker"):
                self.sceneManager.setupCustomOrientationMarker()
            if hasattr(self.sceneManager, "changeViewAxisLabels"):
                self.sceneManager.changeViewAxisLabels()
            # Lighting must run AFTER models + display nodes exist
            # QTimer ensures lighting runs after the renderer has geometry
            if hasattr(self.sceneManager, "setupLighting"):
                qt.QTimer.singleShot(0, self.sceneManager.setupLighting)

            # Enforce black background in all 3D views
            qt.QTimer.singleShot(0, self.setThreeDViewBackgroundToBlack)

        # ------------------------------------------------------------------
        # OpenIGTLink connection (Slicer 5.7+ timing-safe)
        #
        # Slicer 5.7:
        #   OpenIGTLinkIF typically registers vtkMRMLIGTLConnectorNode early,
        #   so connector creation usually succeeds immediately.
        #
        # Slicer 5.10+:
        #   The OpenIGTLinkIF module may be loaded, but its MRML node classes
        #   can be registered later in the startup sequence. We therefore:
        #     1) verify that the module is present, and
        #     2) verify that the node class is actually creatable,
        #   before attempting to create the EPCMR connector node.
        # ------------------------------------------------------------------
        app = slicer.app
        mm = app.moduleManager()
        igtl_module = mm.module("OpenIGTLinkIF")

        # If the module is missing, do not attempt connector creation.
        if igtl_module is None or not hasattr(slicer.modules, "openigtlinkif"):
            logging.warning(
                "EPCMR: OpenIGTLinkIF module is not available in this Slicer instance. "
                "Skipping automatic IGTL connector creation."
            )
            return

        # Probe: try to create a temporary IGTL connector node.
        # If this fails, the node class is not yet registered.
        probe = slicer.mrmlScene.AddNewNodeByClass("vtkMRMLIGTLConnectorNode", "IGTL_PROBE_EPCMR")
        if probe is None:
            logging.error(
                "EPCMR: OpenIGTLinkIF is loaded, but vtkMRMLIGTLConnectorNode "
                "is not registered yet. Cannot create IGTL connector node."
            )
            return
        else:
            # Remove probe node again to keep the scene clean.
            slicer.mrmlScene.RemoveNode(probe)

        # Safe to create or retrieve the actual EPCMR connector node.
        cnode = slicer.mrmlScene.GetFirstNodeByName("IGTLConnector_EPCMR")
        if not cnode:
            cnode = slicer.mrmlScene.AddNewNodeByClass("vtkMRMLIGTLConnectorNode", "IGTLConnector_EPCMR")

        if cnode is None:
            logging.error(
                "EPCMR: Failed to create IGTL connector node even though vtkMRMLIGTLConnectorNode is creatable in this scene."
            )
            return

        cnode.SetTypeClient("localhost", 18944)
        if cnode.GetState() == slicer.vtkMRMLIGTLConnectorNode.StateOff:
            cnode.Start()
            logging.info("EPCMR: IGTL connector started (localhost:18944).")

        # Collapse DataProbe at startup
        def ensureDataProbeCollapsed():
            mw = slicer.util.mainWindow()
            if not mw:
                return
            dp = slicer.util.findChild(mw, "DataProbeCollapsibleWidget")
            if dp:
                dp.collapsed = True
                dp.setVisible(True)
                return
            qt.QTimer.singleShot(100, ensureDataProbeCollapsed)

        qt.QTimer.singleShot(0, ensureDataProbeCollapsed)

        slicer.util.forceRenderAllViews()

        # Attach markups observers NOW that mappingPts and ablationPts exist.
        if self.sceneManager:
            try:
                self.sceneManager.setupMarkupsObservers()
                logging.info("EPCMR: Markups observers attached after scene setup.")
            except Exception as e:
                logging.error(f"EPCMR: Failed to attach markups observers: {e}")

        logging.info("EPCMR: Scene initialized via Logic and SceneManager.")

    # ------------------------------------------------------------------
    # 6. Scene traffic controller
    # ------------------------------------------------------------------
    @vtk.calldata_type(vtk.VTK_OBJECT)  # type: ignore
    def onNodeAdded(self, caller, event, callData):
        """
        Traffic controller for newly added scene nodes (Drag-and-Drop).

        Corrected for new architecture:
          - No SubjectHierarchy warnings
          - No RemoveNode() calls
          - No ghost nodes
          - Drag-and-drop restore uses the SAME restore-safe pipeline
            as the GUI "Restore Backup..." button.
        """
        if not callData:
            return

        # ------------------------------------------------------------------
        # DRAG-AND-DROP BACKUP RESTORE (mappingPts_*/ablationPts_* .mrk.json)
        # ------------------------------------------------------------------
        if callData.IsA("vtkMRMLMarkupsFiducialNode"):
            name = callData.GetName() or ""

            # Only handle drag-and-drop AFTER EPCMR scene is initialized
            # (SceneManager + ModelObserver + persistent markups must exist)
            try:
                pNodeWrapped = self.getParameterNode()
            except Exception:
                pNodeWrapped = None

            mappingPts = getattr(pNodeWrapped, "mappingPts", None) if pNodeWrapped else None
            ablationPts = getattr(pNodeWrapped, "ablationPts", None) if pNodeWrapped else None

            if not (self.sceneManager and self.modelObserver and mappingPts and ablationPts):
                # EPCMR not fully initialized yet -> ignore drag-and-drop
                logging.info("EPCMR: Drag-and-drop ignored (scene not initialized yet).")
                return

            # Detect ONLY backup nodes, not the persistent ones
            if name.startswith("mappingPts_") or name.startswith("ablationPts_"):
                logging.info(f"EPCMR: Drag-and-drop backup detected: {name}")

                # ------------------------------------------------------------------
                # IMPORTANT:
                #   We do NOT restore directly here.
                #   We do NOT remove the dropped node.
                #   We simply pass the file path to the SAME restore pipeline
                #   used by the GUI button (_doRestoreBackup).
                #
                #   The dropped node is a vtkMRMLMarkupsFiducialNode created
                #   by Slicer from the .mrk.json file. We extract its storage
                #   node path and call _doRestoreBackup(filePath).
                #
                #   This avoids:
                #     - SubjectHierarchy inconsistencies
                #     - DataNodeCache errors
                #     - GetItemDataNode warnings
                #     - race conditions
                # ------------------------------------------------------------------

                storageNode = callData.GetStorageNode()
                if storageNode:
                    filePath = storageNode.GetFileName()
                    if filePath and filePath.lower().endswith(".mrk.json"):
                        # Defer restore to next event loop cycle
                        qt.QTimer.singleShot(0, lambda p=filePath: self._doRestoreBackup(p))

                # DO NOT REMOVE callData (the dropped node)
                # Instead, make it inert so SH does not complain.
                try:
                    callData.SetDisplayVisibility(False)
                    callData.SetSaveWithScene(False)
                except Exception:
                    pass

                return

        # ------------------------------------------------------------------
        # Anatomy models
        # ------------------------------------------------------------------
        if callData.IsA("vtkMRMLModelNode"):
            name = callData.GetName()

            # Guard against cloned RA
            if "Cloned" in name:
                if self.sceneManager and self.sceneManager.pNode:
                    self.sceneManager.pNode.raClonedModel = callData
                return

            if not any(x in name for x in ["Abl", "Ref", "Marker", "Pts", "LUT"]):
                if self.sceneManager and hasattr(self.sceneManager, "autoColorAnatomy"):
                    wasAnatomy = self.sceneManager.autoColorAnatomy(callData)

                    if wasAnatomy and self.sceneManager.pNode:
                        if name.startswith("RightAtriumCardiac") and "Cloned" not in name:
                            # ORIGINAL anatomical RA model:
                            # name MUST start with "RightAtriumCardiac"
                            self.sceneManager.pNode.raModel = callData
                            if hasattr(self, "modelObserver") and self.modelObserver:
                                self.modelObserver.RA = callData

                        elif "RightVentricle" in name:
                            self.sceneManager.pNode.rvModel = callData

                    if wasAnatomy:
                        return

            # Catheters: Abl / Ref - apply clinical styling for black background
            if any(x in name for x in ["Abl", "Ref"]):
                if self.sceneManager and hasattr(self.sceneManager, "enhanceCatheterAppearance"):
                    self.sceneManager.enhanceCatheterAppearance(callData)
                return

        # ------------------------------------------------------------------
        # Volumes / DICOM
        # ------------------------------------------------------------------
        elif callData.IsA("vtkMRMLVolumeNode"):
            self.onDICOMVolumeAdded(callData)

    def _restoreFromDroppedMarkups(self, tempNode):
        """
        Restore mappingPts or ablationPts from a drag-and-drop .mrk.json file.

        Behavior:
          - Only accepts nodes whose names start with 'mappingPts_' or 'ablationPts_'
          - Uses the dropped node's points directly (no re-load from disk)
          - Copies points into the persistent EPCMRParameterNode markups
          - Re-wires ModelObserver to the persistent mappingPts
          - Sets the active markups list to the persistent node (for DELETE)
          - Reapplies clinical styling for mappingPts / ablationPts
          - Triggers a single final interpolation + heatmap update for mappingPts
          - Removes the dropped node after restore to avoid clutter
        """
        if not tempNode or not tempNode.IsA("vtkMRMLMarkupsFiducialNode"):
            return

        name = tempNode.GetName() or ""
        baseName = name.split("_")[0]

        # Strict EPCMR naming convention for backups
        if baseName not in ("mappingPts", "ablationPts"):
            logging.warning(f"EPCMR: _restoreFromDroppedMarkups called for unsupported baseName: {baseName}")
            return

        logging.info(f"EPCMR: Drag-and-drop restore for {baseName} from node '{name}'")

        # Resolve wrapped parameter node and persistent target node
        try:
            pNodeWrapped = self.getParameterNode()
        except Exception as e:
            logging.error(f"EPCMR: Failed to get wrapped parameter node during drag-and-drop restore: {e}")
            return

        persistentNode = getattr(pNodeWrapped, baseName, None)
        if not persistentNode:
            logging.info(f"EPCMR: Persistent node for {baseName} not available yet; ignoring drag-and-drop restore.")
            return

        # Helper: enforce numeric labels
        def sanitize_label(raw, fallback="0.0"):
            if raw is None:
                return fallback
            s = raw.strip().replace(",", ".")
            try:
                float(s)
                return s
            except Exception:
                return fallback

        sceneManager = getattr(self, "sceneManager", None)
        modelObserver = getattr(self, "modelObserver", None)

        # Suppress storms during restore (if flags exist)
        mec = getattr(modelObserver, "mappingEventController", None) if modelObserver else None
        if mec and hasattr(mec, "isRestoringBackup"):
            mec.isRestoringBackup = True
        if sceneManager:
            if hasattr(sceneManager, "isRestoringBackup"):
                sceneManager.isRestoringBackup = True
            if hasattr(sceneManager, "suppressBackup"):
                sceneManager.suppressBackup = True

        slicer.mrmlScene.StartState(slicer.vtkMRMLScene.BatchProcessState)

        def doRestore():
            try:
                # Disable auto-label formatting BEFORE adding points
                dn = persistentNode.GetDisplayNode()
                if dn:
                    try:
                        dn.SetDefaultLabelFormat("")
                        dn.SetControlPointLabelFormat("")
                    except Exception:
                        pass

                # Clear existing points
                persistentNode.RemoveAllControlPoints()

                # Copy points with numeric-only labels from the dropped node
                for i in range(tempNode.GetNumberOfControlPoints()):
                    pos = [0, 0, 0]
                    tempNode.GetNthControlPointPosition(i, pos)
                    raw_label = tempNode.GetNthControlPointLabel(i)
                    clean_label = sanitize_label(raw_label)

                    idx = persistentNode.AddControlPoint(pos)
                    persistentNode.SetNthControlPointLabel(idx, clean_label)

                # Make sure the persistent node is the active list (for DELETE, etc.)
                slicer.modules.markups.logic().SetActiveListID(persistentNode)

                # Re-wire ModelObserver to the persistent mappingPts if needed
                if baseName == "mappingPts" and modelObserver and hasattr(modelObserver, "setAndObserveMappingNode"):
                    try:
                        modelObserver.setAndObserveMappingNode(persistentNode)
                    except Exception as e:
                        logging.error(f"EPCMR: Failed to re-wire ModelObserver to mappingPts after restore: {e}")

                # Reapply clinical styling via ShortcutManager (CRITICAL for visual consistency)
                try:
                    widgetRep = slicer.modules.epcmr.widgetRepresentation()
                    widget = widgetRep.self()
                    sm = getattr(widget, "shortcutManager", None)
                    if sm:
                        if baseName == "mappingPts":
                            sm.applyMappingPointStyle(persistentNode)
                        else:
                            sm.applyAblationPointStyle(persistentNode)
                except Exception as e:
                    logging.error(f"EPCMR: Failed to reapply {baseName} style after restore: {e}")

            finally:
                slicer.mrmlScene.EndState(slicer.vtkMRMLScene.BatchProcessState)

                # Re-enable flags
                if mec and hasattr(mec, "isRestoringBackup"):
                    mec.isRestoringBackup = False
                if sceneManager:
                    if hasattr(sceneManager, "isRestoringBackup"):
                        sceneManager.isRestoringBackup = False
                    if hasattr(sceneManager, "suppressBackup"):
                        sceneManager.suppressBackup = False

                # Remove the dropped node AFTER restore to avoid clutter
                try:
                    slicer.mrmlScene.RemoveNode(tempNode)
                except Exception as e:
                    logging.warning(f"EPCMR: Failed to remove dropped backup node '{name}': {e}")

                # Final interpolation + heatmap only for mappingPts
                if baseName == "mappingPts":

                    def finalize_restore():
                        node = persistentNode
                        if not node or node.GetNumberOfControlPoints() < 2:
                            logging.info("EPCMR: finalize_restore aborted (mappingPts has < 2 points).")
                            return

                        # One final interpolation
                        try:
                            if modelObserver and hasattr(modelObserver, "runInterpolation"):
                                modelObserver.runInterpolation(node)
                                logging.info("EPCMR: runInterpolation executed after drag-and-drop restore.")
                        except Exception as e:
                            logging.error(f"EPCMR: Interpolation after drag-and-drop restore failed: {e}")

                        # One final legend / colormap update
                        try:
                            if sceneManager and hasattr(sceneManager, "updateRightAtrialColormap"):
                                sceneManager.updateRightAtrialColormap()
                                logging.info("EPCMR: updateRightAtrialColormap executed after drag-and-drop restore.")
                        except Exception as e:
                            logging.error(f"EPCMR: Legend update after drag-and-drop restore failed: {e}")

                        slicer.util.forceRenderAllViews()

                    qt.QTimer.singleShot(0, finalize_restore)

        qt.QTimer.singleShot(0, doRestore)

    def onDICOMVolumeAdded(self, volumeNode):
        """Starts the debounce timer for volume/DICOM imports."""
        self.syncTimer.start(500)
        logging.debug(f"EPCMR: Volume detected ({volumeNode.GetName()}). Syncing shortly...")

    def resetThreeDViews(self):
        """Resets the camera in all 3D views to fit all visible geometry."""
        layoutManager = slicer.app.layoutManager()
        if not layoutManager:
            return

        for i in range(layoutManager.threeDViewCount):
            threeDWidget = layoutManager.threeDWidget(i)
            if not threeDWidget:
                continue
            threeDView = threeDWidget.threeDView()
            threeDView.resetCamera()

        slicer.util.forceRenderAllViews()
        logging.info("EPCMR: 3D View reset to fit new anatomy/catheters.")

    def setThreeDViewBackgroundToBlack(self):
        """
        Enforces a black background in all 3D views.

        Uses the MRML view nodes so the setting is persistent and
        consistent with Slicer's view architecture.
        """
        layoutManager = slicer.app.layoutManager()
        if not layoutManager:
            return

        # 1) Set default view node background (for any new 3D views)
        defaultViewNode = slicer.mrmlScene.GetDefaultNodeByClass("vtkMRMLViewNode")
        if not defaultViewNode:
            defaultViewNode = slicer.mrmlScene.AddNewNodeByClass("vtkMRMLViewNode")
        defaultViewNode.SetBackgroundColor(0.0, 0.0, 0.0)
        defaultViewNode.SetBackgroundColor2(0.0, 0.0, 0.0)

        # 2) Apply to all existing 3D view nodes
        viewNodes = slicer.util.getNodesByClass("vtkMRMLViewNode")
        for viewNode in viewNodes:
            viewNode.SetBackgroundColor(0.0, 0.0, 0.0)
            viewNode.SetBackgroundColor2(0.0, 0.0, 0.0)

    # ------------------------------------------------------------------
    # 7. LIVE / REPLAY tinting callback
    # ------------------------------------------------------------------
    def onMyTransformModified(self, caller, event):
        """
        Transform-modified callback for Abl_01_TF / Ref_01_TF.

        Responsibilities:
        -----------------
        1. Dynamic visibility initialization:
             - Catheters remain hidden until their FIRST valid matrix arrives.
             - After first valid frame, visibility stays enabled permanently.

        2. Tinting:
             - Forward validity state to SceneManager.updateCatheterVisuals().

        3. Tip-position caching (NEW):
             - Every time a transform updates, extract the OpenIGTLink.tipPos attribute.
             - Cache the latest valid tip position in:
                   self._cachedTipPos  (list of 3 floats)
                   self._cachedTipValid (bool)
             - This cached tip is later used by STRING-triggered mapping point placement.
        """

        pNode = self._parameterNodeWrapped
        now = time.time()

        # ------------------------------------------------------------------
        # Identify catheter type (Abl vs Ref)
        # ------------------------------------------------------------------
        isAbl = "Abl" in caller.GetName()
        catheterKey = "Abl" if isAbl else "Ref"

        # Track last event time (optional diagnostic)
        if isAbl:
            last = getattr(self, "_lastAblEventTime", None)
            self._lastAblEventTime = now
        else:
            last = getattr(self, "_lastRefEventTime", None)
            self._lastRefEventTime = now

        # ------------------------------------------------------------------
        # Disable during replay
        # ------------------------------------------------------------------
        if getattr(pNode, "replayModeActive", False):
            return

        # ------------------------------------------------------------------
        # Validate transform -> model binding
        # ------------------------------------------------------------------
        modelID = caller.GetAttribute("TargetModelID")
        if not modelID:
            return

        targetModel = slicer.mrmlScene.GetNodeByID(modelID)
        if not targetModel:
            return

        # Only respond if this transform actually drives the model
        if targetModel.GetTransformNodeID() != caller.GetID():
            return

        # ------------------------------------------------------------------
        # LIVE update throttle (sender -> IGTL -> Slicer -> EPCMRLogic)
        #
        # Pipeline:
        #   pyigtl sender  ->  OpenIGTLinkIF connector  ->  MRML TransformNode
        #   ->  TransformModifiedEvent  ->  onMyTransformModified()
        #
        # Slicer fires TransformModifiedEvent for *every* incoming IGTL
        # transform message (potentially 100-200 Hz). To prevent EPCMR from
        # being overwhelmed by extremely high?frequency updates (tinting,
        # visibility gating, RA colormap refresh), we apply a lightweight
        # throttle here.
        #
        # The 100 Hz throttle (0.01 s = 10 ms):
        #   - LIVE catheter updates capped at ~100 Hz
        #   - Much smoother motion than the previous 10 Hz limit
        #   - Still protects UI responsiveness under heavy IGTL load
        #
        # Note:
        #   IGTL timestamps are *not* used for scheduling in LIVE mode.
        #   The update rate is determined solely by incoming message rate
        #   and this throttle.
        # ------------------------------------------------------------------
        curr = time.time()
        try:
            last = pNode.lastAblationUpdateTime if isAbl else pNode.lastReferenceUpdateTime
        except AttributeError:
            last = 0.0

        # The 100 Hz throttle
        if (curr - last) < 0.01:
            return

        # Update timestamps on wrapped node
        try:
            if isAbl:
                pNode.lastAblationUpdateTime = curr
            else:
                pNode.lastReferenceUpdateTime = curr
        except Exception:
            pass

        # ------------------------------------------------------------------
        # Determine validity from OpenIGTLink attribute
        # ------------------------------------------------------------------
        isInvalid = str(caller.GetAttribute("OpenIGTLink.valid")).lower() == "false"
        isValid = not isInvalid

        # ------------------------------------------------------------------
        # NEW: Cache tip position for mapping point placement
        #       BUT ONLY for Abl_01_TF (never for Ref_01_TF)
        # ------------------------------------------------------------------
        # isAbl is already computed above as: isAbl = "Abl" in caller.GetName()
        # We now additionally enforce the exact transform name:
        #   - Only accept if this is the main ablation transform "Abl_01_TF"
        # Comment out the name check if you ever add more Abl_* transforms.
        if isAbl and caller.GetName() == "Abl_01_TF":
            tipPosAttr = caller.GetAttribute("OpenIGTLink.tipPos")
            if tipPosAttr:
                try:
                    vals = [float(v) for v in tipPosAttr.strip("[]").replace(" ", "").split(",")]
                    if len(vals) == 3:
                        self._cachedTipPos = vals
                        self._cachedTipValid = True
                    else:
                        self._cachedTipValid = False
                except Exception:
                    self._cachedTipValid = False
            else:
                self._cachedTipValid = False
        else:
            # Any non-Abl_01_TF transform (including Ref_01_TF) must NOT touch the cache
            # We deliberately do nothing here to keep the last valid Abl_01_TF tip.
            pass

        # ------------------------------------------------------------------
        # Dynamic initialization gating (first valid frame -> show catheter)
        # ------------------------------------------------------------------
        if not hasattr(self, "_initializedCatheters"):
            self._initializedCatheters = set()

        displayNode = targetModel.GetDisplayNode()
        if displayNode:
            if catheterKey not in self._initializedCatheters:
                if isValid:
                    # First valid frame -> permanently enable visibility
                    self._initializedCatheters.add(catheterKey)
                    displayNode.SetVisibility(True)
                    displayNode.SetVisibility2D(True)
                else:
                    # Still waiting for first valid frame -> force hidden
                    if displayNode.GetVisibility():
                        displayNode.SetVisibility(False)
                        displayNode.SetVisibility2D(False)
                    return  # Skip tinting until initialized

        # ------------------------------------------------------------------
        # Forward to SceneManager for tinting
        # ------------------------------------------------------------------
        if self.sceneManager and hasattr(self.sceneManager, "updateCatheterVisuals"):
            self.sceneManager.updateCatheterVisuals(targetModel, isValid)

    # ------------------------------------------------------------------
    # 8. Sync timer callback
    # ------------------------------------------------------------------
    def onSyncTimerTimeout(self):
        """Callback triggered after the volume loading 'burst' has settled."""
        try:
            if self.shortcutManager:
                self.shortcutManager.syncParameterNode()
                slicer.util.mainWindow().setFocus(qt.Qt.ActiveWindowFocusReason)
                logging.info("EPCMRLogic: Shortcuts re-synced after data import.")
        except Exception as e:
            logging.error(f"EPCMRLogic: Failed to sync shortcuts: {e}")

    # ------------------------------------------------------------------
    # 9. Cleanup (logic-level)
    # ------------------------------------------------------------------
    def cleanup(self):
        """Remove observers and stop timers to prevent memory leaks."""
        scene = slicer.mrmlScene

        # Scene observers
        if self.sceneObserverTag:
            try:
                scene.RemoveObserver(self.sceneObserverTag)
            except Exception:
                pass
            self.sceneObserverTag = None

        if getattr(self, "sceneEndCloseObserverTag", None):
            try:
                scene.RemoveObserver(self.sceneEndCloseObserverTag)
            except Exception:
                pass
            self.sceneEndCloseObserverTag = None

        if getattr(self, "sceneEndImportObserverTag", None):
            try:
                scene.RemoveObserver(self.sceneEndImportObserverTag)
            except Exception:
                pass
            self.sceneEndImportObserverTag = None

        # Parameter node observer
        if self._pNodeObserver and self._parameterNode:
            try:
                self._parameterNode.RemoveObserver(self._pNodeObserver)
            except Exception:
                pass
            self._pNodeObserver = None

        # Timers
        for timer_attr in ["syncTimer", "viewResetTimer"]:
            if hasattr(self, timer_attr):
                timer = getattr(self, timer_attr)
                if timer:
                    try:
                        timer.stop()
                    except Exception:
                        pass
                    setattr(self, timer_attr, None)

        # ModelObserver
        if self.modelObserver:
            try:
                if hasattr(self.modelObserver, "cleanup"):
                    self.modelObserver.cleanup()
            except Exception as e:
                logging.error(f"EPCMR: ModelObserver cleanup failed: {e}")
            self.modelObserver = None

        # SceneManager
        if self.sceneManager:
            try:
                if hasattr(self.sceneManager, "cleanup"):
                    self.sceneManager.cleanup()
            except Exception as e:
                logging.error(f"EPCMR: SceneManager cleanup failed: {e}")
            self.sceneManager = None

        # ShortcutManager
        if self.shortcutManager:
            try:
                self.shortcutManager.cleanup()
                logging.info("EPCMR: ShortcutManager resources released.")
            except Exception as e:
                logging.debug(f"EPCMR: ShortcutManager cleanup failed: {e}")
            self.shortcutManager = None

        # Catheter initialization + cached tip state
        # Ensure we never carry stale visibility/tip state across sessions.
        try:
            self._initializedCatheters = set()
        except Exception:
            pass
        self._cachedTipPos = None
        self._cachedTipValid = False

    # ------------------------------------------------------------------
    # Scene closed / reset
    # ------------------------------------------------------------------
    def onSceneClosed(self, caller, event):
        """
        Called when the user closes the scene (Ctrl+W).
        This MUST rebuild the entire EPCMR workflow so a new session can start.

        IMPORTANT (Ctrl+W lifecycle):
        -----------------------------
        EndCloseEvent is fired while Slicer is still tearing down the MRML scene
        and destroying/rebuilding slice and 3D views.

        To avoid deadlocks and crashes:
          - Do NOT touch the main window's update state here.
          - Do NOT apply QGraphicsOpacityEffect or similar here.
          - Only schedule the actual reset to the next event loop cycle.
        """
        logging.info("EPCMRLogic: Scene closed (Ctrl+W) - scheduling EPCMR reset.")

        # Do NOT rebuild immediately. SubjectHierarchy and views are still clearing.
        # Delay to next event loop cycle.
        qt.QTimer.singleShot(0, self._resetEPCMRSession)

    def _resetEPCMRSession(self):
        """
        Perform a complete EPCMR reset AFTER the scene has fully closed
        and SubjectHierarchy has stabilized.

        This method is invoked asynchronously after Ctrl+W via onSceneClosed.
        It performs a full teardown of EPCMR managers/observers and then
        rebuilds the EPCMR workflow for a fresh session.
        """
        logging.info("EPCMRLogic: Performing EPCMR session reset after Ctrl+W.")

        # ------------------------------------------------------------------
        # 0) Full teardown of observers + managers
        # ------------------------------------------------------------------
        self.cleanup()
        # Fresh catheter initialization gate for LIVE mode
        self._initializedCatheters = set()

        # ------------------------------------------------------------------
        # 1) Drop old raw + wrapped parameter nodes
        # ------------------------------------------------------------------
        self._parameterNode = None
        self._parameterNodeWrapped = None

        # ------------------------------------------------------------------
        # 2) Recreate raw + wrapped parameter node
        # ------------------------------------------------------------------
        self.getParameterNode()

        # Reset Mapping Mode to default
        try:
            pNodeWrapped = self.getParameterNode()
            pNodeWrapped.mappingMode = "Activation Time Mapping"
            logging.info("EPCMRLogic: Mapping Mode reset to 'Activation Time Mapping' after scene close (Ctrl+W).")
        except Exception as e:
            logging.error(f"EPCMRLogic: Failed to reset Mapping Mode after scene close (Ctrl+W): {e}")

        # ------------------------------------------------------------------
        # 3) Recreate SceneManager + ModelObserver + scene observers
        # ------------------------------------------------------------------
        try:
            self.setupSceneManager()
        except Exception as e:
            logging.error(f"EPCMRLogic: Failed to recreate SceneManager after scene close (Ctrl+W): {e}")

        # ------------------------------------------------------------------
        # 4) Recreate mappingPts, ablationPts, RA/RV models, catheters, etc.
        # ------------------------------------------------------------------
        try:
            self.setupEPCMRScene()
        except Exception as e:
            logging.error(f"EPCMRLogic: Failed to recreate EPCMR scene after scene close (Ctrl+W): {e}")

        # After clinical scene setup, ensure catheter models are visible again
        try:
            pNodeWrapped = self.getParameterNode()
            abl_model = getattr(pNodeWrapped, "ablModel", None)
            ref_model = getattr(pNodeWrapped, "refModel", None)

            for model in (abl_model, ref_model):
                if model:
                    dn = model.GetDisplayNode()
                    if dn:
                        dn.SetVisibility(True)
                        dn.SetVisibility2D(True)
                        dn.SetOpacity(1.0)
                        dn.Modified()
            logging.info("EPCMRLogic: Catheter models re?enabled after scene reset (Ctrl+W).")
        except Exception as e:
            logging.error(f"EPCMRLogic: Failed to re?enable catheter visibility after scene reset (Ctrl+W): {e}")

        # ------------------------------------------------------------------
        # 5) Replayer is now created LAZILY via logic.getReplayer()
        #
        # IMPORTANT:
        # ----------
        # Logic must NOT instantiate CatheterReplayer directly anymore.
        # The Widget owns replayer creation and calls logic.getReplayer()
        # when the user presses "Show Replayer Controls".
        #
        # Therefore: DO NOT create self.replayer here.
        # ------------------------------------------------------------------
        self.replayer = None

        # ------------------------------------------------------------------
        # 6) Ensure scalar bars are hidden after scene reset
        # ------------------------------------------------------------------
        try:
            if self.sceneManager:
                self.sceneManager.hideAllScalarBars()
                logging.info("EPCMRLogic: All scalar bars hidden after scene reset (Ctrl+W).")
        except Exception as e:
            logging.error(f"EPCMRLogic: Failed to hide scalar bars after reset (Ctrl+W): {e}")

        # ------------------------------------------------------------------
        # 7) Rebind RAFlutterWidget + MappingModeSelector
        #
        # NOTE:
        # -----
        # Replayer is NOT rebound here anymore.
        # RAFlutterWidget will call logic.getReplayer() when needed.
        # ------------------------------------------------------------------
        try:
            wrep = slicer.modules.epcmr.widgetRepresentation()
            mainWidget = wrep.self()
            raWidget = getattr(mainWidget, "raWidget", None)

            if raWidget is not None:
                newPn = self.getParameterNode()

                # Rebind RAFlutterWidget to the fresh wrapped parameter node
                raWidget.pn = newPn

                # Rebind MappingModeSelector to the same node and resync combo
                selector = getattr(raWidget, "mappingModeSelector", None)
                if selector is not None:
                    selector._parameterNode = newPn
                    if hasattr(selector, "_syncFromParameterNode"):
                        selector._syncFromParameterNode()

                # Ensure RAFlutterWidget applies its mode-dependent UI logic
                if hasattr(raWidget, "onMappingModeChanged"):
                    raWidget.onMappingModeChanged(getattr(newPn, "mappingMode", "Activation Time Mapping"))

                logging.info("EPCMRLogic: RAFlutterWidget rebound to new parameter node after scene reset (Ctrl+W).")
            else:
                logging.info("EPCMRLogic: No raWidget found on EPCMRWidget during session reset (Ctrl+W).")

        except Exception as e:
            logging.error(f"EPCMRLogic: Failed to rebind RAFlutterWidget after scene reset (Ctrl+W): {e}")

        # ------------------------------------------------------------------
        # 8) Enforce black background + camera reset for new session
        # ------------------------------------------------------------------
        try:
            self.setThreeDViewBackgroundToBlack()
            self.resetThreeDViews()
            logging.info("EPCMRLogic: 3D view background + camera reset after scene close (Ctrl+W).")
        except Exception as e:
            logging.error(f"EPCMRLogic: Failed to reset 3D views after scene close (Ctrl+W): {e}")

        # ------------------------------------------------------------------
        # 9) Final confirmation
        # ------------------------------------------------------------------
        logging.info("EPCMRLogic: New EPCMR session initialized after scene close (Ctrl+W).")

    def run_process(self):
        """
        UI-triggered recomputation entry point.
        Called by RAFlutterWidget (e.g., clampThreshold slider).
        Delegates interpolation to ModelObserver using the current mappingPts node.
        """
        try:
            pNode = self.getParameterNode()
            markupsNode = getattr(pNode, "mappingPts", None)

            if not markupsNode:
                return

            if hasattr(self, "modelObserver") and self.modelObserver:
                # GeometryInterpolator is owned by ModelObserver in the new architecture.
                if hasattr(self.modelObserver, "runInterpolation"):
                    self.modelObserver.runInterpolation(markupsNode)
        except Exception as e:
            logging.error(f"EPCMRLogic.run_process failed: {e}")


"""
Parameter node access pattern used in EPCMR:

1) GetParameterNode()  -- Slicer API (C++ backed)
   ------------------------------------------------
   This is the real Slicer method implemented in C++.
   It always returns the *raw* MRML parameter node:
       vtkMRMLScriptedModuleNode
   The raw node lives in the MRML scene, fires MRML events,
   participates in scene saving/loading, and must never be
   overridden or shadowed.

2) getParameterNode()  -- EPCMR API (Python wrapper)
   ------------------------------------------------
   EPCMR introduces a second concept: a *wrapped* parameter node
   (EPCMRParameterNode). This wrapper provides typed workflow
   attributes such as:
       ablationPts, mappingPts, ablModel, refModel,
       transforms, lastSavePath, invalidRedTint, etc.

   These fields do NOT exist on the raw MRML node and must not
   be stored there. The wrapper is used by:
       - SceneManager
       - ModelObserver
       - CatheterReplayer
       - Workflow widgets (RAFlutterWidget, PVCWidget)
       - EPCMR UI logic

   Therefore EPCMR exposes its own accessor:
       getParameterNode() -> returns the wrapped EPCMRParameterNode

Summary:
   GetParameterNode()  -> raw MRML node (Slicer, observers, scene)
   getParameterNode()  -> wrapped EPCMRParameterNode (EPCMR workflow)
"""


# --------------------------------------------------------------------------
# 2. EPCMR Main Module Class (Entry Point)
# --------------------------------------------------------------------------
class EPCMR(ScriptedLoadableModule):
    def __init__(self, parent):
        ScriptedLoadableModule.__init__(self, parent)

        self.parent.title = "EPCMR"
        self.parent.categories = ["Cardiac"]
        self.parent.helpText = (
            "<b>EPCMR - Electrophysiology CMR Toolkit</b><br><br>"
            "--- Multi-procedure EP-CMR suite for RA Flutter and PVC Ablation ---<br><br>"
            "This module provides tools for RA Flutter and PVC ablation workflows.<br><br>"
            "<b>Help & Acknowledgement</b><br>"
            "Please see the Acknowledgement section for contributors, institutions, and funding."
        )

        # self.parent.contributors = [
        #     "Ingo Paetsch (Heart Center Leipzig)",
        #     "Cosima Jahnke (Heart Center Leipzig)",
        #     "Christian Stehning (Philips Research Laboratories)",
        #     "Sascha Krueger (Philips Research Laboratories)",
        #     "Sandra Haltmeier (ETH Zurich)",
        #     "Max Fuetterer (ETH Zurich)",
        #     "Sebastian Kozerke (ETH Zurich)",
        # ]

        self.parent.acknowledgementText = (
            "<b>A Joint Engineering & Development Effort of:</b><br>"
            "<table border='0' cellspacing='0' cellpadding='0' style='margin-top: 5px; margin-left: 20px;'>"
            "<tr>"
            "<td align='center' valign='top'><b><font size='4'>Heart Center Leipzig</font></b></td>"
            "<td width='30' align='center' valign='top'><font size='4'>&nbsp;|&nbsp;</font></td>"
            "<td align='center' valign='top'><b><font size='4'>Philips Research</font></b></td>"
            "<td width='30' align='center' valign='top'><font size='4'>&nbsp;|&nbsp;</font></td>"
            "<td align='center' valign='top'><b><font size='4'>ETH Zurich</font></b></td>"
            "</tr>"
            "<tr>"
            "<td align='center' valign='top'><small>Leipzig, Germany</small></td>"
            "<td></td>"
            "<td align='center' valign='top'><small>Eindhoven, The Netherlands<br>Hamburg, Germany</small></td>"
            "<td></td>"
            "<td align='center' valign='top'><small>Zurich, Switzerland</small></td>"
            "</tr>"
            "</table><br>"
            "<br>"
            "<b>Core Developers/Authors:</b>"
            "<ul>"
            "<li><b>Ingo Paetsch & Cosima Jahnke</b> (Heart Center Leipzig)</li>"
            "<li><b>Christian Stehning, Sascha Krueger, Steffen Weiss & Jouke Smink</b> (Philips Research)</li>"
            "<li><b>Sandra Haltmeier, Max Fuetterer & Sebastian Kozerke</b> (ETH Zurich)</li>"
            "</ul>"
            "<b>Funding:</b>"
            "<ul>"
            "<li>Swiss National Science Foundation (SNSF), Grant No. 10002638</li>"
            "</ul>"
            "<b>Citation:</b>"
            "<ul>"
            "<li>Please cite the following publications if you use EPCMR in your research:</li>"
            "</ul>"
            "<ol style='margin-left: 20px; margin-top: -5px;'>"
            "<li>Paetsch I, Sommer P, Jahnke C, Hilbert S, Loebe S, Schoene K, Oebel S, Krueger S, Weiss S, Smink J, Lloyd T, Hindricks G. <i>Clinical workflow and applicability of electrophysiological cardiovascular magnetic resonance-guided radiofrequency ablation of isthmus-dependent atrial flutter.</i> Eur Heart J Cardiovasc Imaging. 2019 Feb 1;20(2):147-156. DOI: 10.1093/ehjci/jey143. PMID: 30307544.</li>"
            "<li style='margin-top: 5px;'>Jahnke C, Darma A, Lindemann F, Oebel S, Hilbert S, Bode K, Stehning C, Smink J, Paetsch I. <i>Electrophysiological cardiovascular MR: procedure-ready mesh model generation for interventional guidance based on non-selective excitation compressed sensing whole heart imaging.</i> Sci Rep. 2024 Apr 18;14(1):8974. DOI: 10.1038/s41598-024-59230-0. PMID: 38637577; PMCID: PMC11026457.</li>"
            "<li style='margin-top: 5px;'>Stehning C, Krueger S, Weiss S, Smink J, Koken P, Hindricks G, Jahnke C, Paetsch I. <i>Silent active device tracking for MR-guided interventional procedures.</i> Magn Reson Med. 2023 May;89(5):2005-2013. DOI: 10.1002/mrm.29567. PMID: 36585913.</li>"
            "</ol>"
            "<br>"
        )

        # EPCMR (module class): Owns the single EPCMRLogic instance
        # Instantiate logic here to fix the "Ghost" bug
        self.logic = EPCMRLogic()
        # NOTE: But the utility (!) is case-insensitive or maps to the internal 'epcmr' key
        # self.logic = slicer.util.getModuleLogic("epcmr")

        # Add shortcut to toolbar once Slicer finishes loading
        if not slicer.app.commandOptions().noMainWindow:
            slicer.app.connect("startupCompleted()", self.registerModuleToolBarButton)

    def registerModuleToolBarButton(self):
        """Adds the EPCMR icon to the main Slicer toolbar."""
        mw = slicer.util.mainWindow()
        if not mw:
            return
        mainToolBar = slicer.util.findChild(mw, "ModuleToolBar")
        if not mainToolBar:
            return

        actionName = "EPCMRModuleToolBarButton"
        for action in mainToolBar.actions():
            if action.objectName == actionName:
                return

        iconPath = os.path.join(os.path.dirname(__file__), "Resources", "Icons", "EPCMR.png")
        icon = qt.QIcon(iconPath) if os.path.exists(iconPath) else qt.QIcon()
        moduleAction = mainToolBar.addAction(icon, "EPCMR")
        moduleAction.setObjectName(actionName)
        moduleAction.setToolTip("Switch to EPCMR module")
        moduleAction.triggered.connect(lambda: slicer.util.selectModule("EPCMR"))


# --------------------------------------------------------------------------
# 3. EPCMR Widget (UI)
# --------------------------------------------------------------------------
class EPCMRWidget(ScriptedLoadableModuleWidget):
    # ----------------------------------------------------------------------
    # Optional: pyigtl installation check (for external sender scripts only)
    # ----------------------------------------------------------------------
    def _ensure_pyigtl(self):
        import logging

        if ensure_pyigtl_installed():
            logging.info("EPCMR: pyigtl is available (used only by external sender).")
        else:
            slicer.util.errorDisplay(
                "EPCMR: 'pyigtl' could not be installed. "
                "External IGTL sender scripts may not run, but Slicer IGTL reception is unaffected."
            )

    # ----------------------------------------------------------------------
    # IGTL STRING integration via scene NodeAddedEvent (vtkMRMLTextNode)
    # ----------------------------------------------------------------------
    def _setupStringNodeAddedObserver(self):
        """
        Observe the MRML scene for vtkMRMLTextNode nodes created by OpenIGTLink
        STRING messages (e.g. device_name 'mappingLabel' or 'ActivationLabel').

        This replaces the previous ModifiedEvent-based approach, which does not
        fire for vtkMRMLTextNode text changes in Slicer 5.6+.
        """

        # Remove previous scene observer if any
        if hasattr(self, "_sceneObserverTag") and self._sceneObserverTag:
            try:
                slicer.mrmlScene.RemoveObserver(self._sceneObserverTag)
            except Exception:
                pass
            self._sceneObserverTag = None

        try:
            self._sceneObserverTag = slicer.mrmlScene.AddObserver(
                slicer.vtkMRMLScene.NodeAddedEvent,
                self._onNodeAdded_StringMessage,
            )
            logging.info("EPCMRWidget: STRING NodeAddedEvent observer attached.")
        except Exception as e:
            logging.error(f"EPCMRWidget: Failed to add STRING NodeAddedEvent observer: {e}")
            self._sceneObserverTag = None

    @vtk.calldata_type(vtk.VTK_OBJECT)
    def _onNodeAdded_StringMessage(self, caller, event, callData):
        """
        Called whenever ANY MRML node is added.
        We filter for vtkMRMLTextNode created by OpenIGTLink STRING messages.
        """
        node = callData
        if node is None or not hasattr(node, "IsA"):
            return

        # Only handle TextNodes (STRING messages)
        if not node.IsA("vtkMRMLTextNode"):
            return

        name = node.GetName() if hasattr(node, "GetName") else ""
        if not name:
            return

        # Match device_name used by the sender
        if "mappingLabel" not in name and "ActivationLabel" not in name:
            return

        node_id = node.GetID()

        # Delay reading the text because Slicer populates it AFTER NodeAddedEvent
        def delayed_process():
            n = slicer.mrmlScene.GetNodeByID(node_id)
            if not n:
                return

            try:
                label = n.GetText()
            except Exception:
                label = None

            if not label:
                logging.warning("EPCMRWidget: STRING TextNode still empty after delay.")
                return

            label = str(label).strip()
            if not label:
                logging.warning("EPCMRWidget: STRING TextNode whitespace-only after delay.")
                return

            logging.info(f"EPCMRWidget: Received STRING label '{label}' (delayed).")

            # Forward to EPCMR mapping handler
            try:
                self._handleIncomingMappingLabel(label)
            except Exception as e:
                logging.error(f"EPCMRWidget: Error in _handleIncomingMappingLabel('{label}'): {e}")

            # Remove the temporary node
            self._safeRemoveNode(node_id)

        # Schedule delayed processing (text will be available)
        qt.QTimer.singleShot(20, delayed_process)

    def _safeRemoveNode(self, node_id):
        """
        Safely removes a temporary MRML node (e.g. TextNode created by IGTL STRING)
        after the OpenIGTLink pipeline has finished processing it.
        """
        node = slicer.mrmlScene.GetNodeByID(node_id)
        if node:
            slicer.mrmlScene.RemoveNode(node)

    # ----------------------------------------------------------------------
    # STRING-triggered mapping point placement using cached tip
    # ----------------------------------------------------------------------
    def _handleIncomingMappingLabel(self, label):
        """
        Called when a STRING/TEXT message arrives.
        Uses the cached tip position maintained by EPCMRLogic.onMyTransformModified().
        """
        logic = self.logic

        if not hasattr(logic, "_cachedTipValid") or not logic._cachedTipValid:
            logging.error("EPCMRWidget: STRING/TEXT received but no valid cached tipPos.")
            return

        tip = logic._cachedTipPos

        if hasattr(self, "shortcutManager") and self.shortcutManager:
            try:
                self.shortcutManager.AddMapPointWithCachedTip(label, tip)
            except Exception as e:
                logging.error(f"EPCMRWidget: Failed to add mapping point for label '{label}': {e}")

        logging.info(f"[HANDLE] label={label}, cachedTipValid={logic._cachedTipValid}, tip={logic._cachedTipPos}")

    # ----------------------------------------------------------------------
    # Widget setup
    # ----------------------------------------------------------------------
    def setup(self):
        """
        EPCMRWidget.setup()

        Architectural rule:
        -------------------
        The Module class (EPCMR) owns the logic instance.
        The Widget retrieves and uses it.
        """

        if hasattr(self, "_initialized") and self._initialized:
            return
        self._initialized = True

        super().setup()

        # ------------------------------------------------------------------
        # Logic instance
        # ------------------------------------------------------------------
        self.logic = EPCMRLogic()
        print(f"DEBUG: Logic initialized: {self.logic}")

        # Parameter node + SceneManager
        pNodeWrapped = self.logic.getParameterNode()
        self._parameterNode = pNodeWrapped
        self.logic.setupSceneManager()

        # ------------------------------------------------------------------
        # Procedure Selection
        # ------------------------------------------------------------------
        selectionCollapsibleButton = ctk.ctkCollapsibleButton()
        selectionCollapsibleButton.text = "Procedure Selection"
        self.layout.addWidget(selectionCollapsibleButton)

        selectionLayout = qt.QFormLayout(selectionCollapsibleButton)

        self.procedureSelector = qt.QComboBox()
        self.procedureSelector.addItems(["RA Flutter Ablation", "PVC Ablation"])
        selectionLayout.addRow("Active Workflow:", self.procedureSelector)

        # Free Angulator launcher button
        self.freeAngulatorButton = qt.QPushButton("Open Free Angulator")
        self.freeAngulatorButton.toolTip = "Open the Free Angulator floating panel for slice angulation and geometry storage."
        selectionLayout.addRow("Free Angulation:", self.freeAngulatorButton)
        self.freeAngulatorButton.clicked.connect(self.onOpenFreeAngulator)

        # ------------------------------------------------------------------
        # Stacked widget for submodules
        # ------------------------------------------------------------------
        self.widgetStack = qt.QStackedWidget()
        self.layout.addWidget(self.widgetStack)

        self.procedureSelector.currentIndexChanged.connect(self.onProcedureSelected)

        # ------------------------------------------------------------------
        # Shortcut Manager
        # ------------------------------------------------------------------
        from EPCMRLib.Utilities.ShortcutManager import ShortcutManager

        self.shortcutManager = ShortcutManager(slicer.util.mainWindow(), self._parameterNode)

        # ------------------------------------------------------------------
        # Delayed clinical scene setup
        # ------------------------------------------------------------------
        qt.QTimer.singleShot(500, lambda: self.logic.setupEPCMRScene())

        # ------------------------------------------------------------------
        # Delayed submodule initialization
        # ------------------------------------------------------------------
        qt.QTimer.singleShot(750, self.initSubModules)

        self.layout.addStretch(1)

        # Optional: pyigtl installation check
        qt.QTimer.singleShot(0, self._ensure_pyigtl)

        # ------------------------------------------------------------------
        # IGTL STRING observer wiring (scene-based NodeAddedEvent)
        # ------------------------------------------------------------------
        qt.QTimer.singleShot(250, self._setupStringNodeAddedObserver)

        print("--- Sandbox Reload Successful! ---")

    # ----------------------------------------------------------------------
    # Free Angulator integration
    # ----------------------------------------------------------------------
    def onOpenFreeAngulator(self):
        """
        Create and show the Free Angulator floating panel.
        Ensures only one instance exists.
        """
        if not hasattr(self, "_freeAngulatorWidget") or self._freeAngulatorWidget is None:
            try:
                from EPCMRLib.FreeAngulator.FreeAngulatorWidget import FreeAngulatorWidget

                self._freeAngulatorWidget = FreeAngulatorWidget()
            except Exception as e:
                logging.error(f"EPCMRWidget: Failed to create FreeAngulatorWidget: {e}")
                self._freeAngulatorWidget = None
                return

        self._freeAngulatorWidget.show()
        self._freeAngulatorWidget.raise_()
        self._freeAngulatorWidget.activateWindow()

    # ----------------------------------------------------------------------
    # Submodule Synchronization
    # ----------------------------------------------------------------------
    def syncReplayerButtons(self):
        if self._parameterNode:
            self._parameterNode.replayModeActive = False

        for w in (getattr(self, "raWidget", None), getattr(self, "pvcWidget", None)):
            if not w:
                continue
            btn = getattr(w, "launchReplayerButton", None)
            if not btn:
                continue
            btn.blockSignals(True)
            btn.setChecked(False)
            btn.setText("Show Replayer Controls")
            btn.blockSignals(False)

        logging.info("EPCMRWidget: Sub-widget replayer buttons synchronized (Reset to Off).")

    # ----------------------------------------------------------------------
    # Submodule Initialization
    # ----------------------------------------------------------------------
    def initSubModules(self):
        if not self._parameterNode:
            logging.error("EPCMR: Cannot init submodules without ParameterNode.")
            return

        try:
            from EPCMRLib.RAFlutter.RAFlutterWidget import RAFlutterWidget

            self.raWidget = RAFlutterWidget(
                self.logic,
                self._parameterNode,
                mainWidget=self,
                getReplayer=self.logic.getReplayer,
            )

            try:
                from EPCMRLib.PVCAblation.PVCWidget import PVCWidget

                self.pvcWidget = PVCWidget(
                    self.logic,
                    self._parameterNode,
                    mainWidget=self,
                    getReplayer=self.logic.getReplayer,
                )
            except ImportError:
                placeholder = qt.QLabel("PVC Ablation module not available.")
                placeholder.launchReplayerButton = None
                placeholder.onLaunchReplayer = lambda *a, **k: None
                self.pvcWidget = placeholder

            self.widgetStack.addWidget(self.raWidget)
            self.widgetStack.addWidget(self.pvcWidget)

            logging.info("EPCMR: Sub-modules and widgets initialized successfully.")

        except Exception as e:
            logging.error(f"EPCMR: Sub-module load failed: {e}")
            import traceback

            logging.error(traceback.format_exc())

    # ----------------------------------------------------------------------
    # Workflow Switching
    # ----------------------------------------------------------------------
    def onProcedureSelected(self, index):
        if self.widgetStack and self._parameterNode:
            self.widgetStack.setCurrentIndex(index)
            self.syncReplayerButtons()
            self._parameterNode.activeWorkflowIndex = index

    # ----------------------------------------------------------------------
    # Cleanup
    # ----------------------------------------------------------------------
    def cleanup(self):
        """Invoked when the module is closed or reloaded."""

        # ------------------------------------------------------------
        # Detach SCENE observer (NodeAddedEvent for STRING TextNodes)
        # ------------------------------------------------------------
        if hasattr(self, "_sceneObserverTag") and self._sceneObserverTag:
            try:
                slicer.mrmlScene.RemoveObserver(self._sceneObserverTag)
            except Exception:
                pass
            self._sceneObserverTag = None

        # ------------------------------------------------------------
        # Shortcut manager cleanup
        # ------------------------------------------------------------
        if hasattr(self, "shortcutManager") and self.shortcutManager:
            try:
                self.shortcutManager.cleanup()
            except Exception:
                pass

        # ------------------------------------------------------------
        # Logic cleanup
        # ------------------------------------------------------------
        if hasattr(self, "logic") and self.logic:
            try:
                self.logic.cleanup()
            except Exception as e:
                logging.error(f"EPCMR: Logic cleanup failed: {e}")

        # ------------------------------------------------------------
        # Replayer cleanup
        # ------------------------------------------------------------
        if hasattr(self, "replayer") and self.replayer:
            try:
                self.replayer.cleanup()
            except Exception as e:
                logging.error(f"EPCMR: Replayer cleanup failed: {e}")

        # ------------------------------------------------------------
        # Free Angulator cleanup
        # ------------------------------------------------------------
        if hasattr(self, "_freeAngulatorWidget") and self._freeAngulatorWidget:
            try:
                self._freeAngulatorWidget.close()
            except Exception:
                pass
            self._freeAngulatorWidget = None

        # ------------------------------------------------------------
        # Final widget cleanup
        # ------------------------------------------------------------
        self.widgetStack = None

        logging.info("EPCMR: Widget cleanup completed.")

    # ----------------------------------------------------------------------
    # Reload
    # ----------------------------------------------------------------------
    def onReload(self):
        try:
            self.cleanup()
        except Exception as e:
            logging.error(f"EPCMR: Cleanup during reload failed: {e}")

        import importlib

        importlib.invalidate_caches()

        for moduleName in list(sys.modules.keys()):
            if moduleName.startswith("EPCMRLib"):
                del sys.modules[moduleName]

        logging.info("EPCMR: EPCMRLib cache cleared. Reloading scripted module...")
        slicer.util.reloadScriptedModule("EPCMR")

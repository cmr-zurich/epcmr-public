# ruff: isort: skip_file
import os
import time
import logging

import qt
import slicer
import vtk

from EPCMRLib.EPCMRParameterNode import EPCMRParameterNode
from EPCMRLib.Utilities.MaterialManager import MaterialManager
from EPCMRLib.Utilities.LightsManager import LightsManager

VTK_VERSION = (vtk.vtkVersion.GetVTKMajorVersion(), vtk.vtkVersion.GetVTKMinorVersion())

# ---------------------------------------------------------------------------
# EPCMR Architecture: SceneManager <-> MaterialManager
#
# SceneManager:
#   - Orchestrates high-level scene behavior.
#   - Decides which anatomical or clinical nodes should receive rim glow,
#     overlays, normalization, or resets.
#   - Owns ANATOMY_MAP, lighting rig, scalar bars, markups observers,
#     backups, and RA/voltage mapping pipelines.
#   - Delegates all material-related operations to MaterialManager.
#
# MaterialManager:
#   - Executes rim glow presets, rim overlays, and material resets.
#   - Provides idempotent, deterministic material operations.
#   - Never decides which nodes to modify; only performs the work requested
#     by SceneManager.
#   - Ensures clean separation of concerns and prevents SceneManager from
#     accumulating rendering or material logic.
#
# Result:
#   - SceneManager remains a pure coordinator.
#   - MaterialManager owns all shading and material responsibilities.
#   - Architecture is symmetric with LightsManager and easier to maintain.
# ---------------------------------------------------------------------------


def is_vtk_at_least(major: int, minor: int) -> bool:
    """
    Central VTK version check.

    Example:
        if is_vtk_at_least(9, 3):
            ...
    """
    return VTK_VERSION >= (major, minor)


class SceneManager:
    """
    Scene-level coordinator.

    Responsibilities:
      - Create and own Sascha's Rainbow (activation LUT) as a singleton
      - Auto-color anatomical models (RA/RV/SVC/IVC) on load
      - Manage markups observers + backup/restore
      - Provide a single hook to recompute RA colormap
        (delegates to GeometryInterpolator via ModelObserver facade)
      - Maintain dual scalar bars (Activation / Voltage) with legends
        reflecting the REAL data range of the RA clone
      - Delegate all rim-glow and material operations to MaterialManager
        (SceneManager orchestrates, MaterialManager executes)

    Restore-safety:
      - During backup restore, isRestoringBackup is set True.
      - Backups are suppressed via suppressBackup.
      - updateRightAtrialColormap() is a no-op while isRestoringBackup is True.
      - A single final legend update is performed after restore completes.
    """

    def __init__(self, wrappedParameterNode):
        """
        SceneManager MUST use the wrapped node exclusively.
        Never wrap here. The wrapper is created once in EPCMRLogic and
        passed by reference so all components share the same instance.
        """
        if type(wrappedParameterNode).__name__ != "EPCMRParameterNode":
            raise TypeError(f"SceneManager expected EPCMRParameterNode wrapper, got {type(wrappedParameterNode)}")

        self.pNode: EPCMRParameterNode = wrappedParameterNode

        # ---------------------------------------------------------
        # Manage Lights
        # ---------------------------------------------------------
        self.lightsManager = LightsManager()

        # ---------------------------------------------------------
        # Manage Rim Shading/Glow
        # ---------------------------------------------------------
        # Rim glow is now handled by MaterialManager (idempotent)
        # SceneManager only orchestrates which nodes receive rim glow.
        self.materialManager = MaterialManager(self)
        self.rimGlowEnabled = False

        # ---------------------------------------------------------
        # Rim glow is now handled by MaterialManager (idempotent)
        # SceneManager only orchestrates which nodes receive rim glow.
        self.materialManager = MaterialManager(self)
        self.rimGlowEnabled = False

        # ---------------------------------------------------------
        # Material defaults (centralized)
        # ---------------------------------------------------------
        # These defaults favor high diffuse, low ambient, modest specular,
        # and full opacity so scene lights produce visible shading.
        self._materialDefaults = {
            "ambient": 0.02,
            "diffuse": 0.95,
            "specular": 0.03,
            "power": 10.0,
            "opacity": 1.0,
            # optional multiplicative scale for runtime tuning
            "scale": getattr(self, "_materialDefaultsScale", 1.0),
        }

        # ---------------------------------------------------------
        # Sascha's Rainbow procedural color node (singleton)
        # ---------------------------------------------------------
        self.ctf = self.get_ctf()

        singletonTag = "SaschasRainbow"
        self.colorTableNode = slicer.mrmlScene.GetSingletonNode(
            singletonTag,
            "vtkMRMLProceduralColorNode",
        )

        if not self.colorTableNode:
            self.colorTableNode = slicer.mrmlScene.AddNewNodeByClass(
                "vtkMRMLProceduralColorNode",
                "SaschasRainbow",
            )
            self.colorTableNode.SetSingletonTag(singletonTag)
            self.colorTableNode.SetType(slicer.vtkMRMLColorTableNode.User)

        # Attach the CTF
        self.colorTableNode.SetAndObserveColorTransferFunction(self.ctf)

        # High-resolution internal table for smooth activation gradients
        self.colorTableNode.SetNumberOfTableValues(4096)

        self.colorTableNode.SetHideFromEditors(False)
        self.colorTableNode.Modified()

        # Expose activation LUT for ModelObserver / ColorMapper
        self.activationColorNode = self.colorTableNode

        # ---------------------------------------------------------
        # Voltage LUT (legacy fallback - ColorMapper owns the real one)
        # ---------------------------------------------------------
        voltageNode = slicer.mrmlScene.GetFirstNodeByName("EPCMR_VoltageLUT")
        if not voltageNode:
            voltageNode = slicer.mrmlScene.AddNewNodeByClass(
                "vtkMRMLColorTableNode",
                "EPCMR_VoltageLUT",
            )
            voltageNode.SetTypeToUser()
            # Simple 256-step grayscale as a safe default
            num_colors = 256
            voltageNode.SetNumberOfColors(num_colors)
            for i in range(num_colors):
                g = float(i) / float(num_colors - 1)
                voltageNode.SetColor(i, f"{g:.3f}", g, g, g, 1.0)
            voltageNode.Modified()

        self.voltageColorNode = voltageNode

        # ---------------------------------------------------------
        # Color Legends (scalar bars) - dual, mode-specific
        # ---------------------------------------------------------
        self.activationScalarBarActor = None
        self.activationScalarBarWidget = None

        self.voltageScalarBarActor = None
        self.voltageScalarBarWidget = None

        # ---------------------------------------------------------
        # Anatomy Color Configuration
        # ---------------------------------------------------------
        self.ANATOMY_MAP = {
            # NOTE: CARTO-style bluish neutral like 'NEUTRAL_CLONE_COLOR'/'RA_COLOR'
            "RA": {
                # MUST match both "rightatrium" and "rightatriumcardiac"
                "keywords": ["rightatrium", "rightatriumcardiac"],
                "color": (102 / 255, 115 / 255, 140 / 255),
                "attr": "raModel",
            },
            "RV": {
                # MUST match both "rightventricle" and "rightventriclecardiac"
                "keywords": ["rightventricle", "rightventriclecardiac"],
                "color": (205 / 255, 20 / 255, 120 / 255),
                "attr": "rvModel",
            },
            "SVC": {
                # "svc" already matches "svc bj 78465309"
                "keywords": ["svc", "superior vena"],
                "color": (140 / 255, 110 / 255, 20 / 255),
                "attr": None,
            },
            "IVC": {
                # "ivc" already matches "ivc bj 78465309"
                "keywords": ["ivc", "inferior vena"],
                "color": (200 / 255, 190 / 255, 160 / 255),
                "attr": None,
            },
        }

        self.CATHETER_CONFIG = {
            "Ref": {"colors": {0: (0, 0.39, 0), 1: (0, 0.39, 0), 2: (1, 1, 0), 3: (0, 1, 0)}},
            "Abl": {"colors": {0: (0.39, 0, 0), 1: (0.39, 0, 0), 2: (1, 1, 0), 3: (1, 0, 0)}},
        }

        self._markupObserverTags = {}
        self._saveTimers = {}

        # Backup + restore-safety flags
        self.suppressBackup = False
        self.isRestoringBackup = False

    # ------------------------------------------------------------------
    # Rim Shading/Glow
    # ------------------------------------------------------------------

    # Reset preset (neutral Slicer defaults)
    def resetRimGlow(self, modelNode):
        dn = modelNode.GetDisplayNode()
        if not dn:
            return

        # PRIOR SETTTINGS
        # dn.SetBackfaceCulling(False)
        # dn.SetAmbient(0.10)
        # dn.SetDiffuse(0.90)
        # dn.SetSpecular(0.10)
        # dn.SetPower(10)
        # # Do NOT touch opacity here - leave anatomy opacity unchanged

        dn.SetBackfaceCulling(False)
        # apply defaults but preserve opacity
        self.applyMaterialDefaultsToNode(modelNode, preserveOpacity=True)

    def applyMaterialDefaultsToNode(self, modelNode, preserveOpacity=False):
        """
        Apply centralized material defaults to a model's display node.
        preserveOpacity: if True, do not override the node's existing opacity.
        """
        if not modelNode:
            return
        modelNode.CreateDefaultDisplayNodes()
        dn = modelNode.GetDisplayNode()
        if not dn:
            return

        defaults = dict(self._materialDefaults)  # copy
        scale = float(defaults.get("scale", 1.0))

        try:
            ambient = float(defaults.get("ambient", 0.02))
            diffuse = float(defaults.get("diffuse", 0.95))
            specular = float(defaults.get("specular", 0.03))
            power = float(defaults.get("power", 10.0))
            opacity = float(defaults.get("opacity", 1.0))
        except Exception:
            # fallback safe values
            ambient, diffuse, specular, power, opacity = 0.02, 0.95, 0.03, 10.0, 1.0

        # apply scale to diffuse/specular only (keeps ambient small)
        diffuse = max(0.0, min(1.0, diffuse * scale))
        specular = max(0.0, min(1.0, specular * scale))

        try:
            dn.SetAmbient(ambient)
        except Exception:
            pass
        try:
            dn.SetDiffuse(diffuse)
        except Exception:
            pass
        try:
            dn.SetSpecular(specular)
        except Exception:
            pass
        try:
            dn.SetPower(power)
        except Exception:
            pass
        if not preserveOpacity:
            try:
                dn.SetOpacity(opacity)
            except Exception:
                pass

        # Ensure lighting/shading are enabled for anatomy by default
        try:
            dn.SetLighting(True)
            dn.SetShading(True)
        except Exception:
            pass

        # Ensure scalar visibility is off by default for anatomy unless explicitly used
        try:
            if not dn.GetActiveScalarName():
                dn.SetScalarVisibility(False)
        except Exception:
            pass

    def applyRimGlowToAnatomy(self):
        keywords = []
        for entry in self.ANATOMY_MAP.values():
            keywords.extend([k.lower() for k in entry["keywords"]])

        for node in slicer.util.getNodesByClass("vtkMRMLModelNode"):
            name = node.GetName().lower()
            if any(k in name for k in keywords):
                # Delegate to MaterialManager
                self.materialManager.applyRimGlow(node)

    def resetRimGlowOnAnatomy(self):
        keywords = []
        for entry in self.ANATOMY_MAP.values():
            keywords.extend([k.lower() for k in entry["keywords"]])

        for node in slicer.util.getNodesByClass("vtkMRMLModelNode"):
            name = node.GetName().lower()
            if any(k in name for k in keywords):
                # Delegate to MaterialManager
                self.materialManager.resetRimMaterial(node)

    def toggleRimGlow(self):
        if self.rimGlowEnabled:
            # Full restore via MaterialManager
            self.materialManager.restoreAllAnatomyAppearance()
            self.rimGlowEnabled = False
            print("EPCMR: Rim glow disabled.")
        else:
            # Full apply via MaterialManager
            self.materialManager.boostRimGlowOnAllAnatomy()
            self.rimGlowEnabled = True
            print("EPCMR: Rim glow enabled.")

    # ------------------------------------------------------------------
    # Catheter appearance
    # ------------------------------------------------------------------
    def enhanceCatheterAppearance(self, modelNode):
        """
        Clinical catheter styling for black-background visibility.
        Guarantees that Abl/Ref catheters remain visible even when
        anatomy is brightly illuminated.
        """
        if not modelNode:
            return

        modelNode.CreateDefaultDisplayNodes()
        dn = modelNode.GetDisplayNode()
        if not dn:
            return

        # === CRITICAL FIX FOR SELF-LIT EMISSIVE QUALITY ===
        # Turning ScalarVisibility OFF breaks the VTK internal hardware link that
        # forces scene light tracking. This allows the catheter to become 100% self-lit.
        try:
            dn.SetScalarVisibility(False)
        except Exception:
            pass
        try:
            dn.SetLighting(False)  # Turn off scene lighting equations completely
        except Exception:
            pass

        # === REAL-TIME CARTO LIGHTING: Controlled Emissive Performance ===
        # Use a controlled emissive baseline rather than full 1.0 to avoid overexposure.
        # Per-catheter scale allows runtime tuning without changing lights.
        base_ambient = 0.60  # controlled emissive baseline (was 1.00)
        base_diffuse = 0.00
        base_specular = 0.02
        base_power = 2.0

        # Read optional per-node emissive scale attribute (string) if present
        try:
            attr = dn.GetAttribute("EPCMR_CatheterEmissiveScale")
            scale = float(attr) if attr is not None else 1.0
        except Exception:
            scale = 1.0

        # Clamp scale to a safe range
        try:
            scale = max(0.4, min(1.6, float(scale)))
        except Exception:
            scale = 1.0

        emissive = max(0.0, min(1.0, base_ambient * scale))

        try:
            dn.SetAmbient(emissive)  # controlled self-illumination
        except Exception:
            pass
        try:
            dn.SetDiffuse(base_diffuse)
        except Exception:
            pass
        try:
            dn.SetSpecular(base_specular)
        except Exception:
            pass
        try:
            dn.SetPower(base_power)
        except Exception:
            pass

        # === ELECTRODE SEAMS: High-speed edge definition ===
        try:
            dn.SetEdgeVisibility(True)
        except Exception:
            pass
        try:
            dn.SetEdgeColor(0.10, 0.10, 0.10)  # Sharp dark seams distinctly separate electrode bands
        except Exception:
            pass
        try:
            dn.SetLineWidth(1.5)  # Crisp, lightweight separation lines
        except Exception:
            pass

        try:
            dn.SetBackfaceCulling(False)
        except Exception:
            pass

        # === FIXED CARTO VISIBILITY: Force Catheters to Render Over Internal Shadows ===
        # Explicitly configure the backend graphics card mappers across all 3D viewports
        # to ensure that raw emissive vectors are drawn without shadow interference.
        try:
            lm = slicer.app.layoutManager()
            if lm:
                for i in range(lm.threeDViewCount):
                    threeDWidget = lm.threeDWidget(i)
                    if not threeDWidget:
                        continue
                    renderer = threeDWidget.threeDView().renderWindow().GetRenderers().GetFirstRenderer()
                    if not renderer:
                        continue

                    props = renderer.GetViewProps()
                    props.InitTraversal()
                    p = props.GetNextProp()
                    while p:
                        if hasattr(p, "GetMapper"):
                            m = p.GetMapper()
                            try:
                                if m and m.GetInput() == modelNode.GetPolyData():
                                    # Force drop lighting calculations on the GPU mapper
                                    try:
                                        m.SetLighting(False)
                                    except Exception:
                                        pass
                                    try:
                                        m.SetColorModeToDirectScalars()
                                    except Exception:
                                        pass
                                    try:
                                        m.ScalarVisibilityOff()
                                    except Exception:
                                        pass
                            except Exception:
                                # Some mapper implementations may raise on GetInput comparison
                                pass
                        p = props.GetNextProp()
        except Exception:
            pass

        if is_vtk_at_least(9, 3):
            try:
                dn.SetInterpolationToPhong()
            except AttributeError:
                pass

        # --- ENABLE FXAA FOR SMOOTH EDGES ---
        try:
            lm = slicer.app.layoutManager()
            if lm:
                for i in range(lm.threeDViewCount):
                    view = lm.threeDWidget(i).threeDView()
                    rw = view.renderWindow()
                    renderer = rw.GetRenderers().GetFirstRenderer()

                    # FXAA ON (works with emissive geometry)
                    try:
                        if hasattr(renderer, "UseFXAAOn"):
                            renderer.UseFXAAOn()
                        elif hasattr(renderer, "SetUseFXAA"):
                            renderer.SetUseFXAA(1)
                    except Exception:
                        pass

            # Phong interpolation improves curvature before FXAA
            try:
                if is_vtk_at_least(9, 3):
                    dn.SetInterpolationToPhong()
            except Exception:
                pass
        except Exception:
            pass

        # Flush changes instantly to the GPU renderer with zero geometric overhead
        slicer.util.forceRenderAllViews()

    # ------------------------------------------------------------------
    # Sascha's Rainbow (activation CTF)
    # ------------------------------------------------------------------
    def get_ctf(self):
        """
        Correct Sascha's Rainbow:

        - vtkDiscretizableColorTransferFunction
        - 7 EPCMR-original RGB points
        - continuous (DiscretizeOff)
        - internal 128-sample resolution
        - range 0.0-0.5 (critical for Shepard interpolation)
        """
        ctf = vtk.vtkDiscretizableColorTransferFunction()
        ctf.SetColorSpaceToRGB()
        ctf.SetScaleToLinear()

        # EPCMR original rainbow
        ctf.AddRGBPoint(0.0000, 0.5586, 0.0000, 0.0000)
        ctf.AddRGBPoint(0.0547, 0.9805, 0.0000, 0.0000)
        ctf.AddRGBPoint(0.1797, 0.9961, 0.9805, 0.0000)
        ctf.AddRGBPoint(0.3047, 0.0156, 0.9961, 0.9805)
        ctf.AddRGBPoint(0.4297, 0.0000, 0.0156, 0.9961)
        ctf.AddRGBPoint(0.5000, 0.0000, 0.0000, 0.5000)

        ctf.SetNumberOfValues(128)
        ctf.DiscretizeOff()

        # CRITICAL: EPCMR's Shepard interpolation expects this range
        ctf.SetRange(0.0, 0.5)

        return ctf

    # ------------------------------------------------------------------
    # Paths / initialization
    # ------------------------------------------------------------------
    def initializePaths(self):
        """
        Standardizes data storage paths across platforms.
        """
        from datetime import datetime

        home_path = os.path.join(os.path.expanduser("~"), "SlicerEPCMRStudyData")
        if not os.path.exists(home_path):
            os.makedirs(home_path, exist_ok=True)

        if not self.pNode.lastSavePath:
            self.pNode.lastSavePath = home_path
            logging.info(f"SceneManager: Data path set to {home_path}")

        if not hasattr(self, "sessionBackupPath"):
            now = datetime.now()
            sessionStamp = now.strftime("%Y-%m-%d_%H-%M-%S")
            self.sessionBackupPath = os.path.join(self.pNode.lastSavePath, f"backups_{sessionStamp}")
            os.makedirs(self.sessionBackupPath, exist_ok=True)
            logging.info(f"SceneManager: Created session backup folder {self.sessionBackupPath}")

    # ------------------------------------------------------------------
    # Markups observers + backup
    # ------------------------------------------------------------------
    def setupMarkupsObservers(self):
        """
        Registers observers on mappingPts and ablationPts so that backups
        are triggered on point add/remove/modify events.

        One-time per EPCMR session; guarded by _markupsObserversInitialized.
        """
        if getattr(self, "_markupsObserversInitialized", False):
            return

        if not hasattr(self, "_markupObserverTags"):
            self._markupObserverTags = {}

        nodes_to_observe = {
            "ablationPts": getattr(self.pNode, "ablationPts", None),
            "mappingPts": getattr(self.pNode, "mappingPts", None),
        }

        for nodeName, node in nodes_to_observe.items():
            if not node or not slicer.mrmlScene.GetNodeByID(node.GetID()):
                continue

            if nodeName in self._markupObserverTags:
                for tag in self._markupObserverTags[nodeName]:
                    try:
                        node.RemoveObserver(tag)
                    except Exception:
                        pass
            self._markupObserverTags[nodeName] = []

            def onPointAdded(caller, event, name=nodeName):
                if getattr(self.pNode, "replayModeActive", False):
                    return
                self._onMarkupNodeModified(name, action="afterAdd")

            def onPointRemoved(caller, event, name=nodeName):
                if getattr(self.pNode, "replayModeActive", False):
                    return
                self._onMarkupNodeModified(name, action="afterDelete")

            def onPointModified(caller, event, name=nodeName):
                if getattr(self.pNode, "replayModeActive", False):
                    return
                self._onMarkupNodeModified(name, action="afterModify")

            tags = []
            tags.append(node.AddObserver(slicer.vtkMRMLMarkupsNode.PointAddedEvent, onPointAdded))
            tags.append(node.AddObserver(slicer.vtkMRMLMarkupsNode.PointRemovedEvent, onPointRemoved))
            tags.append(node.AddObserver(slicer.vtkMRMLMarkupsNode.PointModifiedEvent, onPointModified))

            self._markupObserverTags[nodeName] = tags
            logging.debug(f"SceneManager: Observers added to {nodeName}")

        self._markupsObserversInitialized = True

    def _onMarkupNodeModified(self, nodeName, action="afterModify"):
        """
        Debounced backup trigger for markups nodes.

        Restore-safety:
          - If suppressBackup is True (e.g., during restore), this is a no-op.
        """
        if getattr(self.pNode, "replayModeActive", False):
            return

        if self.suppressBackup:
            return

        priority = {"afterModify": 1, "afterAdd": 2, "afterDelete": 3}

        if not hasattr(self, "_pendingActions"):
            self._pendingActions = {}

        if nodeName not in self._pendingActions or priority[action] >= priority[self._pendingActions[nodeName]]:
            self._pendingActions[nodeName] = action

        if nodeName not in self._saveTimers:
            timer = qt.QTimer()
            timer.setSingleShot(True)
            timer.setInterval(200)

            def onTimerTimeout(name=nodeName):
                act = self._pendingActions.get(name, "afterModify")
                self.savePointsBackup(name, action=act)
                self._pendingActions[name] = "afterModify"

            timer.timeout.connect(onTimerTimeout)
            self._saveTimers[nodeName] = timer

        self._saveTimers[nodeName].start()

    # ------------------------------------------------------------------
    # View labels
    # ------------------------------------------------------------------
    def changeViewAxisLabels(self):
        """Enforces L, R, P, A, F, H labels on all views."""
        labels = ["L", "R", "P", "A", "F", "H"]
        for className in ["vtkMRMLViewNode", "vtkMRMLSliceNode"]:
            defaultNode = slicer.mrmlScene.GetDefaultNodeByClass(className)
            if not defaultNode:
                defaultNode = slicer.mrmlScene.AddNewNodeByClass(className)
            for i, label in enumerate(labels):
                defaultNode.SetAxisLabel(i, label)

        viewNodes = slicer.util.getNodesByClass("vtkMRMLViewNode")
        viewNodes.extend(slicer.util.getNodesByClass("vtkMRMLSliceNode"))
        for node in viewNodes:
            for i, label in enumerate(labels):
                node.SetAxisLabel(i, label)

    # ------------------------------------------------------------------
    # Auto-color anatomy (RA/RV/SVC/IVC)
    # ------------------------------------------------------------------
    def autoColorAnatomy(self, modelNode):
        """
        Identifies and colors anatomical models based on keywords.
        """
        if not modelNode:
            return False

        name = modelNode.GetName().lower()

        for _chamber, config in self.ANATOMY_MAP.items():
            # Check if the name strictly BEGINS with any of the valid keywords/prefixes
            if any(name.startswith(k) for k in config["keywords"]):
                modelNode.CreateDefaultDisplayNodes()
                dn = modelNode.GetDisplayNode()

                # ------------------------------------------------------------------
                # CORE FIX: ensure anatomical polydata has normals
                # ------------------------------------------------------------------
                try:
                    poly = modelNode.GetPolyData()
                    if poly and poly.GetNumberOfPoints() > 0:
                        normals = vtk.vtkPolyDataNormals()
                        normals.SetInputData(poly)
                        normals.SetFeatureAngle(60.0)
                        normals.SplittingOff()
                        normals.ConsistencyOn()
                        normals.AutoOrientNormalsOn()
                        normals.ComputePointNormalsOn()  # <- FIXED VERSION
                        normals.Update()
                        modelNode.SetAndObservePolyData(normals.GetOutput())
                except Exception:
                    pass

                dn.SetScalarVisibility(False)
                dn.SetColor(*config["color"])
                # apply centralized material defaults but preserve the intended anatomy opacity
                dn.SetOpacity(0.6)
                self.applyMaterialDefaultsToNode(modelNode, preserveOpacity=True)
                dn.SetBackfaceCulling(False)
                dn.SetVisibility(True)

                attr = config["attr"]
                if attr:
                    setattr(self.pNode, attr, modelNode)

                if hasattr(self, "normalizeAnatomyAppearance"):
                    self.normalizeAnatomyAppearance(modelNode)
                if getattr(self, "rimGlowEnabled", False):
                    # Delegate rim glow to MaterialManager
                    self.materialManager.applyRimGlow(modelNode)

                # ------------------------------------------------------------------
                # *** CRITICAL FIX FOR FACETED VS SMOOTH STARTUP ***
                #
                # VTK mappers created BEFORE lighting/material pipeline exists
                # initialize in FLAT shading mode.
                #
                # Reload creates NEW mappers AFTER pipeline exists → smooth shading.
                #
                # Therefore: force mapper REBUILD here, exactly when anatomy appears.
                # ------------------------------------------------------------------
                try:
                    poly = modelNode.GetPolyData()
                    if poly:
                        # Force mapper to rebuild by replacing it
                        mapper = vtk.vtkPolyDataMapper()
                        mapper.SetInputData(poly)

                        # Smooth shading
                        try:
                            mapper.SetInterpolationToPhong()
                        except Exception:
                            try:
                                mapper.SetInterpolation(2)  # 2 = Phong in many VTK builds
                            except Exception:
                                pass

                        # Reattach mapper to all renderers that use this model
                        lm = slicer.app.layoutManager()
                        if lm:
                            for i in range(lm.threeDViewCount):
                                view = lm.threeDWidget(i).threeDView()
                                renderer = view.renderWindow().GetRenderers().GetFirstRenderer()
                                props = renderer.GetViewProps()
                                props.InitTraversal()
                                p = props.GetNextProp()
                                while p:
                                    if hasattr(p, "GetMapper"):
                                        m = p.GetMapper()
                                        try:
                                            if m and m.GetInput() == poly:
                                                p.SetMapper(mapper)
                                        except Exception:
                                            pass
                                    p = props.GetNextProp()
                except Exception:
                    pass

                # Flush to renderer so mapper replacement takes effect immediately
                try:
                    slicer.util.forceRenderAllViews()
                except Exception:
                    pass

                return True

        return False

    def normalizeAnatomyAppearance(self, modelNode):
        """
        Normalize anatomy appearance for consistent lighting.
        Rim effect is provided by lighting only; GLSL shader removed for this build.
        """
        if not modelNode:
            return

        modelNode.CreateDefaultDisplayNodes()
        dn = modelNode.GetDisplayNode()
        if not dn:
            return

        # --- FIX: PROTECT DYNAMIC CARTO-STYLE SCALAR MAPS ---
        if dn.GetActiveScalarName():
            dn.SetScalarVisibility(True)
        else:
            dn.SetScalarVisibility(False)

        dn.SetBackfaceCulling(False)

        # PRIOR SETTINGS
        # dn.SetAmbient(0.45)
        # dn.SetDiffuse(0.85)
        # dn.SetSpecular(0.10)
        # dn.SetPower(10)
        # dn.SetOpacity(0.60)

        # Apply centralized defaults and then set any anatomy-specific overrides
        self.applyMaterialDefaultsToNode(modelNode, preserveOpacity=False)

        # keep the normalized anatomy opacity target
        try:
            dn.SetOpacity(0.60)
        except Exception:
            pass

        dn.SetLighting(True)
        dn.SetShading(True)

        # ------------------------------------------------------------------
        # *** FIX: FORCE PHONG INTERPOLATION (Gouraud caused faceting) ***
        # ------------------------------------------------------------------
        try:
            if hasattr(dn, "SetInterpolationToPhong"):
                dn.SetInterpolationToPhong()
            else:
                dn.SetInterpolation(2)  # fallback for older VTK
        except Exception:
            pass

        polyData = modelNode.GetPolyData()
        if not polyData:
            return

        # ------------------------------------------------------------------
        # Normals recomputation (safe + deterministic)
        # ------------------------------------------------------------------
        normals = vtk.vtkPolyDataNormals()
        normals.SetInputData(polyData)
        normals.SetSplitting(0)
        normals.SetConsistency(1)
        normals.SetAutoOrientNormals(1)
        normals.SetComputePointNormals(1)  # <-- fixed
        normals.SetComputeCellNormals(0)
        normals.Update()

        polyData.ShallowCopy(normals.GetOutput())
        polyData.Modified()
        modelNode.Modified()

    def normalizeAllAnatomyAppearance(self):
        """
        Apply normalizeAnatomyAppearance to all recognized anatomy models.
        Recognition is based on current naming conventions; colors stay as-is.
        """
        scene = slicer.mrmlScene
        for modelNode in scene.GetNodesByClass("vtkMRMLModelNode"):
            name = modelNode.GetName() or ""
            if "rightatrium" in name or "rightventricle" in name or "svc" in name or "ivc" in name:
                try:
                    self.normalizeAnatomyAppearance(modelNode)
                except Exception as e:
                    logging.error(f"EPCMR: normalizeAnatomyAppearance failed for {name}: {e}")

    def boostRimGlowOnAllAnatomy(self):
        """
        Apply rim-glow to anatomy models using self.ANATOMY_MAP as the source of truth.

        Expected ANATOMY_MAP format (examples):
          {
            "RA": {"keywords": ["rightatrium", "rightatriumcardiac"], "color": (r,g,b), "attr": "raModel"},
            "IVC": {"keywords": ["ivc","inferior vena"], "color": (r,g,b), "attr": None},
            ...
          }

        Behavior:
          - For each canonical key in ANATOMY_MAP, gather alias tokens from 'keywords'.
          - Match model nodes by tokenized name or substring match against aliases.
          - If an 'attr' is provided and self.<attr> resolves to a node or node name/ID, that node is included.
          - Rim glow is ALWAYS re-applied (idempotent), no permanent skip.
          - Calls self.boostRimGlow(node, color=...) if color is provided, otherwise self.boostRimGlow(node).
          - Calls self._applyStrongRimOverlay(node) (idempotent overlay).
          - Returns list of affected node names.
        """
        # Delegate to MaterialManager, preserving behavior
        return self.materialManager.boostRimGlowOnAllAnatomy()

    # ------------------------------------------------------------------
    # Rim-glow reversal and anatomy appearance restoration
    # ------------------------------------------------------------------
    def resetRimGlowOnAllAnatomy(self):
        """
        Reverse of boostRimGlowOnAllAnatomy:
          - Removes rim material.
          - Removes rim overlay.
          - Clears rim-glow tag.
          - Uses ANATOMY_MAP as source of truth.
        """
        return self.materialManager.resetRimGlowOnAllAnatomy()

    def restoreAllAnatomyAppearance(self):
        """
        Full restore:
          - Removes rim glow material.
          - Removes rim overlays.
          - Clears rim tags.
          - Restores default lighting via lightsManager.
          - Normalizes anatomy appearance.
        """
        return self.materialManager.restoreAllAnatomyAppearance()

    # ------------------------------------------------------------------
    # Voltage mapping on RA clone
    # ------------------------------------------------------------------
    def applyVoltageMapToRAClone(self, raCloneNode, voltageArrayName="Voltage"):
        """
        Apply voltage mapping to the RA clone with correct shading and normals.
        """
        if not raCloneNode:
            return

        raCloneNode.CreateDefaultDisplayNodes()
        dn = raCloneNode.GetDisplayNode()
        if not dn:
            return

        polyData = raCloneNode.GetPolyData()
        if not polyData:
            return

        pd = polyData.GetPointData()
        if not pd or not pd.GetArray(voltageArrayName):
            return

        dn.SetScalarVisibility(True)
        dn.SetActiveScalarName(voltageArrayName)
        dn.SetScalarRangeFlag(dn.UseDataScalarRange)

        dn.SetAmbient(0.15)
        dn.SetDiffuse(0.85)
        dn.SetLighting(True)
        dn.SetShading(True)
        dn.SetBackfaceCulling(False)

        if is_vtk_at_least(9, 3):
            try:
                dn.SetInterpolationToPhong()
            except AttributeError:
                pass

        normals = vtk.vtkPolyDataNormals()
        normals.SetInputData(polyData)
        normals.SplittingOff()
        normals.ConsistencyOn()
        normals.AutoOrientNormalsOn()
        normals.Update()

        polyData.ShallowCopy(normals.GetOutput())
        polyData.Modified()
        raCloneNode.Modified()

    # ------------------------------------------------------------------
    # Renderer + lighting
    # ------------------------------------------------------------------
    def _getRenderer(self):
        """
        Returns the ACTIVE 3D view renderer.
        """
        lm = slicer.app.layoutManager()
        if not lm:
            return None

        try:
            threeDWidget = lm.threeDWidget(0)
            threeDView = threeDWidget.threeDView()
            rw = threeDView.renderWindow()
            renderer = rw.GetRenderers().GetFirstRenderer()
            return renderer
        except Exception:
            return None

    # -----------------------------------------------------------------------------
    # Catheter appearance normalization (Safe MRML-based display setup)
    # -----------------------------------------------------------------------------

    def normalizeCatheterAppearance(self, modelNode, emissive=False):
        """
        Normalize catheter appearance for consistent lighting and shading.

        NOTE: normalizeCatheterAppearance() is the authoritative styling function
        for catheter geometry (Abl / Ref), including lighting, shading, normals,
        FXAA, and interpolation model enforcement.

        This function also defines the shading interpolation model (Gouraud/Phong)
        used for catheter rendering. Phong is enforced here to ensure smooth
        curvature shading and consistent visual behavior across GPUs.
        """

        if not modelNode:
            return

        # Ensure a display node container exists without breaking structural scene maps during cold starts
        if not modelNode.GetDisplayNode():
            modelNode.CreateDefaultDisplayNodes()

        dn = modelNode.GetDisplayNode()
        if not dn:
            return

        dn.SetBackfaceCulling(False)
        dn.SetFrontfaceCulling(False)

        # Plastic + glow material (HCl-style glossy cylinder)
        dn.SetLighting(True)
        dn.SetShading(True)

        dn.SetAmbient(0.15)  # was 0.55
        dn.SetDiffuse(0.95)  # was 0.35
        dn.SetSpecular(0.45)

        if emissive:
            # --- ADJUSTED TO REDUCE OVERALL GLOW/BRIGHTNESS ---
            dn.SetAmbient(0.40)  # Lowered from 0.78 to blend shadows back in cleanly
            dn.SetDiffuse(0.60)  # Increased from 0.45 to react nicely to layout lights
            dn.SetSpecular(0.30)  # Lowered from 0.45 to eliminate harsh pinpoint glare

        # Compute explicit surface normal fields to smooth out cylinder edges
        polyData = modelNode.GetPolyData()
        if polyData:
            normals = vtk.vtkPolyDataNormals()
            normals.SetInputData(polyData)
            normals.SplittingOff()
            normals.ConsistencyOn()
            normals.AutoOrientNormalsOn()
            normals.Update()
            polyData.ShallowCopy(normals.GetOutput())
            polyData.Modified()
            modelNode.Modified()

        # --- ENABLE FXAA + PHONG FOR SMOOTH EDGES ---
        try:
            lm = slicer.app.layoutManager()
            if lm:
                for i in range(lm.threeDViewCount):
                    view = lm.threeDWidget(i).threeDView()
                    rw = view.renderWindow()
                    renderer = rw.GetRenderers().GetFirstRenderer()

                    # FXAA ON (works with emissive geometry)
                    try:
                        if hasattr(renderer, "UseFXAAOn"):
                            renderer.UseFXAAOn()
                        elif hasattr(renderer, "SetUseFXAA"):
                            renderer.SetUseFXAA(1)
                    except Exception:
                        pass

            # Phong interpolation improves curvature before FXAA
            try:
                # --- AUTHORITATIVE PHONG ENFORCEMENT FOR CATHETERS ---
                if hasattr(dn, "SetInterpolationToPhong"):
                    dn.SetInterpolationToPhong()
                else:
                    dn.SetInterpolation(2)  # fallback for older VTK
            except Exception:
                pass

        except Exception:
            pass

    def normalizeAllCathetersEmissive(self):
        modelNodes = slicer.util.getNodesByClass("vtkMRMLModelNode")
        for node in modelNodes:
            name = node.GetName() or ""
            if "Abl" in name or "Ref" in name:
                self.normalizeCatheterAppearance(node, emissive=True)

    # ------------------------------------------------------------------
    # Scalar bar helpers (dual legends)
    # ------------------------------------------------------------------
    def _ensureActivationScalarBar(self):
        """
        Ensure activation scalar bar + widget exist and are wired
        with a vtkLookupTable. LUT content is filled in updateRightAtrialColormap().
        """
        if self.activationScalarBarActor and self.activationScalarBarWidget:
            return

        renderer = self._getRenderer()
        if not renderer:
            logging.warning("SceneManager: No renderer available for activation scalar bar")
            return

        interactor = renderer.GetRenderWindow().GetInteractor()
        if not interactor:
            logging.warning("SceneManager: No interactor available for activation scalar bar")
            return

        actor = vtk.vtkScalarBarActor()
        actor.SetOrientationToVertical()
        actor.SetTitle("LAT [ms]")
        actor.SetNumberOfLabels(5)
        actor.SetTextPositionToSucceedScalarBar()
        actor.SetLabelFormat("%.0f")
        actor.SetMaximumNumberOfColors(256)

        lut = vtk.vtkLookupTable()
        lut.SetNumberOfTableValues(256)
        lut.SetRampToLinear()
        lut.Build()
        actor.SetLookupTable(lut)

        titleProperty = actor.GetTitleTextProperty()
        titleProperty.SetFontSize(12)
        titleProperty.SetFontFamilyToCourier()
        titleProperty.BoldOff()
        titleProperty.ItalicOff()
        titleProperty.ShadowOff()
        titleProperty.SetColor(1, 1, 1)
        titleProperty.SetJustificationToLeft()
        actor.SetTextPad(0)

        labelProperty = actor.GetLabelTextProperty()
        labelProperty.SetFontSize(12)
        labelProperty.SetFontFamilyToCourier()
        labelProperty.BoldOff()
        labelProperty.ItalicOff()
        labelProperty.ShadowOff()

        actor.UnconstrainedFontSizeOn()

        actor.DrawBackgroundOn()
        bg = actor.GetBackgroundProperty()
        bg.SetColor(0.0, 0.0, 0.0)
        bg.SetOpacity(0.4)
        actor.DrawFrameOff()

        widget = vtk.vtkScalarBarWidget()
        widget.SetScalarBarActor(actor)
        widget.SetInteractor(interactor)
        widget.SetEnabled(1)
        widget.RepositionableOff()

        rep = widget.GetScalarBarRepresentation()
        rep.SetPosition(0.82, 0.25)
        rep.SetPosition2(0.11, 0.60)

        actor.SetBarRatio(0.60)
        actor.SetVerticalTitleSeparation(15)
        rep.SetShowBorderToOff()

        widget.SetEnabled(1)
        widget.RepositionableOff()

        self.activationScalarBarActor = actor
        self.activationScalarBarWidget = widget

        renderer.AddActor2D(actor)

    def _ensureVoltageScalarBar(self):
        """
        Ensure voltage scalar bar + widget exist and are wired with a vtkLookupTable.
        LUT content is filled in updateRightAtrialColormap().
        """
        if self.voltageScalarBarActor and self.voltageScalarBarWidget:
            return

        renderer = self._getRenderer()
        if not renderer:
            logging.warning("SceneManager: No renderer available for voltage scalar bar")
            return

        interactor = renderer.GetRenderWindow().GetInteractor()
        if not interactor:
            logging.warning("SceneManager: No interactor available for voltage scalar bar")
            return

        actor = vtk.vtkScalarBarActor()
        actor.SetOrientationToVertical()
        actor.SetTitle("Voltage [mV]")
        actor.SetNumberOfLabels(5)
        actor.SetTextPositionToSucceedScalarBar()
        actor.SetLabelFormat("%.3f")
        actor.SetMaximumNumberOfColors(256)

        lut = vtk.vtkLookupTable()
        lut.SetNumberOfTableValues(256)
        lut.SetRampToLinear()
        lut.Build()
        actor.SetLookupTable(lut)

        titleProperty = actor.GetTitleTextProperty()
        titleProperty.SetFontSize(10)
        titleProperty.SetFontFamilyToCourier()
        titleProperty.BoldOff()
        titleProperty.ItalicOff()
        titleProperty.ShadowOff()
        titleProperty.SetColor(1, 1, 1)
        titleProperty.SetJustificationToLeft()
        actor.SetTextPad(0)

        labelProperty = actor.GetLabelTextProperty()
        labelProperty.SetFontSize(12)
        labelProperty.SetFontFamilyToCourier()
        labelProperty.BoldOff()
        labelProperty.ItalicOff()
        labelProperty.ShadowOff()

        actor.UnconstrainedFontSizeOn()

        actor.DrawBackgroundOn()
        bg = actor.GetBackgroundProperty()
        bg.SetColor(0.0, 0.0, 0.0)
        bg.SetOpacity(0.4)
        actor.DrawFrameOff()

        widget = vtk.vtkScalarBarWidget()
        widget.SetScalarBarActor(actor)
        widget.SetInteractor(interactor)
        widget.SetEnabled(1)
        widget.RepositionableOff()

        rep = widget.GetScalarBarRepresentation()
        rep.SetPosition(0.82, 0.25)
        rep.SetPosition2(0.11, 0.60)

        actor.SetVerticalTitleSeparation(15)
        rep.SetShowBorderToOff()

        widget.SetEnabled(1)
        widget.RepositionableOff()

        self.voltageScalarBarActor = actor
        self.voltageScalarBarWidget = widget

        renderer.AddActor2D(actor)

    def _hideActivationScalarBar(self):
        """Hide activation legend without destroying it."""
        if self.activationScalarBarWidget:
            self.activationScalarBarWidget.SetEnabled(0)
        if self.activationScalarBarActor:
            self.activationScalarBarActor.SetVisibility(0)

    def _hideVoltageScalarBar(self):
        """Hide voltage legend without destroying it."""
        if self.voltageScalarBarWidget:
            self.voltageScalarBarWidget.SetEnabled(0)
        if self.voltageScalarBarActor:
            self.voltageScalarBarActor.SetVisibility(0)

    def hideAllScalarBars(self):
        """
        Public helper on SceneManager for "initial view".

        Rationale: EPCMRLogic should not know about individual scalar bar
        widgets; it just asks the SceneManager to "hide all legends".

        Ensure both activation and voltage legends are hidden.
        Used after scene reset to restore the initial view.
        """
        self._hideActivationScalarBar()
        self._hideVoltageScalarBar()

    # ------------------------------------------------------------------
    # Activation mapping on RA clone
    # ------------------------------------------------------------------
    def _applyActivationMapToRAClone(self, raCloneNode):
        """
        Activation-time mapping pipeline for RA clone.
        """
        if not raCloneNode:
            return

        raCloneNode.CreateDefaultDisplayNodes()
        dn = raCloneNode.GetDisplayNode()
        if not dn:
            return

        polyData = raCloneNode.GetPolyData()
        if not polyData:
            return

        pd = polyData.GetPointData()
        if not pd or not pd.GetArray("ActivationTime"):
            dn.SetScalarVisibility(False)
            self.normalizeAnatomyAppearance(raCloneNode)
            return

        dn.SetScalarVisibility(True)
        dn.SetActiveScalarName("ActivationTime")
        dn.SetScalarRangeFlag(dn.UseDataScalarRange)

        dn.SetAmbient(0.15)
        dn.SetDiffuse(0.85)
        dn.SetLighting(True)
        dn.SetShading(True)
        dn.SetBackfaceCulling(False)

        if is_vtk_at_least(9, 3):
            try:
                dn.SetInterpolationToPhong()
            except AttributeError:
                pass

        normals = vtk.vtkPolyDataNormals()
        normals.SetInputData(polyData)
        normals.SplittingOff()
        normals.ConsistencyOn()
        normals.AutoOrientNormalsOn()
        normals.Update()

        polyData.ShallowCopy(normals.GetOutput())
        polyData.Modified()
        raCloneNode.Modified()
        # Rim shader hook remains here; MaterialManager owns rim glow itself
        self._ensureCARTORimShader(dn)

    # ------------------------------------------------------------------
    # RA colormap hook (legend-only; GeometryInterpolator + ColorMapper own colors)
    # ------------------------------------------------------------------
    def updateRightAtrialColormap(self):
        """
        Update only the scalar bar / legend for the right atrial map.

        Restore-safety:
          - If isRestoringBackup is True, this function is a no-op.
            A single final call is made after restore completes.
        """
        # Suppress legend updates during backup restore to avoid flashing
        if getattr(self, "isRestoringBackup", False):
            return

        if hasattr(self, "setupLighting"):
            self.lightsManager.setupLighting()

        clone = getattr(self.pNode, "raClonedModel", None)
        if not clone:
            self._hideActivationScalarBar()
            self._hideVoltageScalarBar()
            slicer.util.forceRenderAllViews()
            return

        poly = clone.GetPolyData()
        if not poly:
            self._hideActivationScalarBar()
            self._hideVoltageScalarBar()
            slicer.util.forceRenderAllViews()
            return

        pd = poly.GetPointData()
        if not pd:
            self._hideActivationScalarBar()
            self._hideVoltageScalarBar()
            slicer.util.forceRenderAllViews()
            return

        mode = getattr(self.pNode, "mappingMode", "Activation Time Mapping")

        if mode == "Activation Time Mapping":
            array_name = "ActivationTime"
            title = "LAT [ms]"
            colorNode = self.colorTableNode

            arr = pd.GetArray(array_name)
            if not arr or not colorNode:
                self._hideActivationScalarBar()
                self._hideVoltageScalarBar()
                slicer.util.forceRenderAllViews()
                return

            try:
                ctf = colorNode.GetColorTransferFunction()
            except Exception:
                self._hideActivationScalarBar()
                self._hideVoltageScalarBar()
                slicer.util.forceRenderAllViews()
                return

            base_min, base_max = ctf.GetRange()
            self._ensureActivationScalarBar()
            self._hideVoltageScalarBar()
            actor = self.activationScalarBarActor

            min_v, max_v = arr.GetRange()

        else:
            array_name = "Voltage"
            title = "Voltage [mV]"

            try:
                widgetRep = slicer.modules.epcmr.widgetRepresentation()
                ui = widgetRep.self()
                mainLogic = getattr(ui, "logic", None)
                colorMapper = mainLogic.modelObserver.colorMapper
                colorNode = colorMapper.voltageColorNode
                ctf = colorMapper.baseVoltageCTF
            except Exception:
                self._hideActivationScalarBar()
                self._hideVoltageScalarBar()
                slicer.util.forceRenderAllViews()
                return

            arr = pd.GetArray(array_name)
            if not arr or not colorNode or not ctf:
                self._hideActivationScalarBar()
                self._hideVoltageScalarBar()
                slicer.util.forceRenderAllViews()
                return

            base_min, base_max = ctf.GetRange()
            self._ensureVoltageScalarBar()
            self._hideActivationScalarBar()
            actor = self.voltageScalarBarActor

            low = getattr(self.pNode, "voltageLowCutoff", 0.1)
            high = getattr(self.pNode, "voltageHighCutoff", 0.5)
            min_v, max_v = float(low), float(high)

        min_v, max_v = sorted([float(min_v), float(max_v)])
        if max_v == min_v:
            max_v += 1e-6

        base_span = max(base_max - base_min, 1e-6)

        if not actor or not ctf:
            self._hideActivationScalarBar()
            self._hideVoltageScalarBar()
            slicer.util.forceRenderAllViews()
            return

        num_values = 4096
        lut = vtk.vtkLookupTable()
        lut.SetNumberOfTableValues(num_values)
        lut.SetRange(float(min_v), float(max_v))
        lut.SetScaleToLinear()
        lut.Build()

        for i in range(num_values):
            t = float(i) / float(num_values - 1)
            x = base_min + t * base_span
            rgb = [0.0, 0.0, 0.0]
            ctf.GetColor(x, rgb)
            lut.SetTableValue(i, rgb[0], rgb[1], rgb[2], 1.0)

        actor.SetLookupTable(lut)
        actor.SetTitle(title)
        actor.SetNumberOfLabels(5)
        actor.VisibilityOn()
        actor.Modified()

        slicer.util.forceRenderAllViews()

    # ------------------------------------------------------------------
    # Clinical models (catheters) + transforms
    # ------------------------------------------------------------------
    def loadClinicalModels(self, resourcesPath, transformCallback=None):
        """
        Loads catheter models (Abl / Ref), assigns their LUTs, display nodes,
        and transform nodes, and wires transform observers correctly.
        Initializes models as HIDDEN until valid telemetry arrives.
        """
        import os
        import logging
        import slicer
        import vtk

        modelPaths = {
            "Abl": os.path.join(resourcesPath, "newSascha12_FH_red_PartID.vtp"),
            "Ref": os.path.join(resourcesPath, "newSascha12_FH_green_PartID.vtp"),
        }

        slicer.mrmlScene.StartState(slicer.vtkMRMLScene.BatchProcessState)
        try:
            for key, path in modelPaths.items():
                if not os.path.exists(path):
                    logging.warning(f"SceneManager: Missing catheter model file: {path}")
                    continue

                nodeName = f"{key}_01_Model"
                modelNode = slicer.util.getFirstNodeByName(nodeName) or slicer.util.loadModel(path)
                modelNode.SetName(nodeName)

                if key == "Abl":
                    self.pNode.ablModel = modelNode
                else:
                    self.pNode.refModel = modelNode

                modelNode.CreateDefaultDisplayNodes()
                dn = modelNode.GetDisplayNode()

                # --- NEW CORRECTION: INITIALIZE MODELS AS HIDDEN ---
                if dn:
                    dn.SetVisibility(False)  # Hide in 3D Views
                    dn.SetVisibility2D(False)  # Hide in 2D Slice Views

                cfg = self.CATHETER_CONFIG.get(key)
                if dn and cfg:
                    table_name = f"LUT_{key}_01"
                    ct = slicer.mrmlScene.GetFirstNodeByName(table_name) or slicer.mrmlScene.AddNewNodeByClass(
                        "vtkMRMLColorTableNode", table_name
                    )

                    ct.SetTypeToUser()
                    ct.SetNumberOfColors(4)
                    for idx, (r, g, b) in cfg["colors"].items():
                        ct.SetColor(idx, f"P{idx}", r, g, b, 1.0)
                    ct.Modified()

                    dn.SetAndObserveColorNodeID(ct.GetID())
                    dn.SetActiveScalar("PartID", vtk.vtkAssignAttribute.POINT_DATA)
                    dn.SetScalarVisibility(True)
                    dn.SetScalarRangeFlag(slicer.vtkMRMLDisplayNode.UseManualScalarRange)
                    dn.SetScalarRange(0, 3)
                    # dn.SetVisibility2D(True) # Removed to prevent overriding initialization logic
                    dn.SetSliceDisplayModeToProjection()

                    poly = modelNode.GetPolyData()
                    if poly:
                        pd = poly.GetPointData()
                        if pd and pd.HasArray("PartID"):
                            pd.SetActiveScalars("PartID")
                            poly.Modified()

                    dn.Modified()
                    modelNode.Modified()

                tfName = f"{key}_01_TF"
                tn = slicer.util.getFirstNodeByName(tfName) or slicer.mrmlScene.AddNewNodeByClass(
                    "vtkMRMLLinearTransformNode", tfName
                )

                tn.SetAttribute("CatheterType", key)
                modelNode.SetAndObserveTransformNodeID(tn.GetID())
                tn.SetAttribute("TargetModelID", modelNode.GetID())

                if transformCallback:
                    tn.RemoveObservers(slicer.vtkMRMLTransformNode.TransformModifiedEvent)
                    tn.AddObserver(slicer.vtkMRMLTransformNode.TransformModifiedEvent, transformCallback)

                rtfName = f"{key}_01_REPLAY_TF"
                rtn = slicer.util.getFirstNodeByName(rtfName) or slicer.mrmlScene.AddNewNodeByClass(
                    "vtkMRMLLinearTransformNode", rtfName
                )

                rtn.SetAttribute("CatheterType", key)
                rtn.SetSaveWithScene(False)

                if key == "Abl":
                    self.pNode.ablTransform = tn
                    self.pNode.ablReplayTransform = rtn
                else:
                    self.pNode.refTransform = tn
                    self.pNode.refReplayTransform = rtn

        finally:
            slicer.mrmlScene.EndState(slicer.vtkMRMLScene.BatchProcessState)
            slicer.app.processEvents()
            slicer.util.forceRenderAllViews()

    def updateCatheterVisuals(self, modelNode, isValid):
        """
        PURE FUNCTION VERSION -- deterministic for replay.
        Always produces the same visual state for the same (modelNode, isValid).
        """
        if getattr(self.pNode, "replayModeActive", False) and not getattr(self.pNode, "replayerActive", False):
            return

        if not modelNode:
            return

        dn = modelNode.GetDisplayNode()
        if not dn:
            return

        ct = dn.GetColorNode()
        if not ct:
            return

        ct.SetColor(3, "StatusColor", 1.0, 1.0, 1.0, 1.0)

        actualValid = bool(isValid)
        isAbl = "Abl" in modelNode.GetName()

        if actualValid:
            color = (1.0, 0.0, 0.0) if isAbl else (0.0, 1.0, 0.0)
            opacity = 1.0
        else:
            color = self.pNode.invalidRedTint if isAbl else self.pNode.invalidGreenTint
            opacity = 0.7

        ct.SetColor(3, "StatusColor", color[0], color[1], color[2], 1.0)
        dn.SetOpacity(opacity)

        ct.Modified()
        dn.Modified()
        modelNode.Modified()

    def resetCatheterVisuals(self, modelNode):
        """
        Hard reset of all catheter visual state.
        This ensures replay is fully deterministic and symmetric.
        """
        if getattr(self.pNode, "replayModeActive", False) and not getattr(self.pNode, "replayerActive", False):
            return

        if not modelNode:
            return

        dn = modelNode.GetDisplayNode()
        if not dn:
            return

        ct = dn.GetColorNode()
        if not ct:
            return

        dn.SetOpacity(1.0)
        ct.SetColor(3, "StatusColor", 1.0, 1.0, 1.0, 1.0)

        ct.Modified()
        dn.Modified()
        modelNode.Modified()

    # ------------------------------------------------------------------
    # Backup of points
    # ------------------------------------------------------------------
    def savePointsBackup(self, targetNodeName=None, action="afterModify"):
        """
        Integrated EPCMR backup system.

        Restore-safety:
          - If suppressBackup is True (e.g., during restore), this is a no-op.
        """
        from datetime import datetime

        if getattr(self, "suppressBackup", False):
            return

        if not self.pNode or not self.pNode.lastSavePath:
            return

        if targetNodeName:
            point_nodes = {targetNodeName: getattr(self.pNode, targetNodeName, None)}
        else:
            point_nodes = {
                "ablationPts": self.pNode.ablationPts,
                "mappingPts": self.pNode.mappingPts,
            }

        if not hasattr(self, "sessionBackupPath"):
            now = datetime.now()
            sessionStamp = now.strftime("%Y-%m-%d_%H-%M-%S")
            self.sessionBackupPath = os.path.join(self.pNode.lastSavePath, f"backups_{sessionStamp}")
            os.makedirs(self.sessionBackupPath, exist_ok=True)
            logging.info(f"SceneManager: Created session backup folder {self.sessionBackupPath}")

        mode = getattr(self.pNode, "mappingMode", "Activation Time Mapping")
        modeFolder = "Voltage" if mode == "Voltage Mapping" else "ActivationTime"

        phase = getattr(self.pNode, "mappingPhase", "POST")

        modeDir = os.path.join(self.sessionBackupPath, modeFolder)
        phaseDir = os.path.join(modeDir, phase)
        os.makedirs(phaseDir, exist_ok=True)

        for nodeName, node in point_nodes.items():
            if not node or not slicer.mrmlScene.GetNodeByID(node.GetID()):
                continue

            numPoints = node.GetNumberOfControlPoints()
            if numPoints <= 0:
                continue

            liveFilePath = os.path.join(self.sessionBackupPath, f"{nodeName}.mrk.json")
            try:
                slicer.util.saveNode(node, liveFilePath)
                logging.debug(f"SceneManager: Live state saved -> {liveFilePath}")
            except Exception as e:
                logging.error(f"SceneManager: Failed to save live file {liveFilePath}: {e}")

            nodeBackupDir = os.path.join(phaseDir, nodeName)
            os.makedirs(nodeBackupDir, exist_ok=True)

            now = datetime.now()
            timestamp = now.strftime("%Y-%m-%d_%H-%M-%S") + f"-{now.microsecond // 1000:03d}"

            filename = f"{nodeName}_{timestamp}_{numPoints:03d}pts_{action}.mrk.json"
            backupPath = os.path.join(nodeBackupDir, filename)

            try:
                slicer.util.saveNode(node, backupPath)
                logging.info(f"SceneManager: Backup saved -> {backupPath}")
            except Exception as e:
                logging.error(f"SceneManager: Failed to save backup {backupPath}: {e}")

    # ------------------------------------------------------------------
    # Custom orientation marker
    # ------------------------------------------------------------------
    def setupCustomOrientationMarker(self):
        """
        Set up a custom Human.vtp orientation marker.
        """
        moduleDir = os.path.dirname(slicer.util.modulePath("EPCMR"))
        markerPath = os.path.normpath(os.path.join(moduleDir, "Resources", "Human.vtp"))

        if not os.path.exists(markerPath):
            logging.warning(f"SceneManager: Human.vtp not found at {markerPath}")
            return

        reader = vtk.vtkXMLPolyDataReader()
        reader.SetFileName(markerPath)
        reader.Update()
        polydata = reader.GetOutput()

        if polydata is None:
            logging.warning("SceneManager: Human.vtp failed to load")
            return

        scaleTransform = vtk.vtkTransform()
        scaleTransform.Scale(0.009, 0.009, 0.009)

        tf = vtk.vtkTransformPolyDataFilter()
        tf.SetInputData(polydata)
        tf.SetTransform(scaleTransform)
        tf.Update()
        scaledPolyData = tf.GetOutput()

        mapper = vtk.vtkPolyDataMapper()
        mapper.SetInputData(scaledPolyData)

        actor = vtk.vtkActor()
        actor.SetMapper(mapper)

        prop = actor.GetProperty()
        prop.SetColor(0.95, 0.95, 0.95)
        prop.SetAmbient(0.28)
        prop.SetDiffuse(0.52)
        prop.SetSpecular(0.20)

        if not hasattr(self, "_orientationMarkerWidgets"):
            self._orientationMarkerWidgets = []

        lm = slicer.app.layoutManager()

        for i in range(lm.threeDViewCount):
            threeDWidget = lm.threeDWidget(i)
            threeDView = threeDWidget.threeDView()
            renderWindow = threeDView.renderWindow()
            interactor = renderWindow.GetInteractor()

            viewNode = threeDView.mrmlViewNode()
            viewNode.SetOrientationMarkerType(slicer.vtkMRMLAbstractViewNode.OrientationMarkerTypeNone)
            viewNode.SetOrientationMarkerHumanModelNodeID(None)

            omw = vtk.vtkOrientationMarkerWidget()
            omw.SetOrientationMarker(actor)
            omw.SetInteractor(interactor)

            omw.SetViewport(0.75, 0.0, 1.0, 0.25)

            omw.SetEnabled(1)
            omw.InteractiveOff()

            self._orientationMarkerWidgets.append(omw)

        logging.info("SceneManager: Custom Human.vtp orientation marker initialized")

    def cleanup(self):
        """
        Properly detach observers and tear down SceneManager-owned VTK state.

        Invariants after cleanup:
          - No markups observers remain.
          - No backup timers remain.
          - No activation/voltage scalar bar actors remain in the renderer.
          - No SceneManager-installed lights remain in any renderer.
          - No SceneManager-generated catheter glow sheaths remain in the MRML scene.
          - Catheter mappers and display properties are reverted to baseline.
        """
        # --------------------------------------------------------------
        # PRELUDE: Ensure renderer GL/lighting state is healthy before teardown
        # --------------------------------------------------------------
        # Run a non-destructive renderer repair once per SceneManager instance.
        # This prevents attempting to remove or reconfigure lights when the
        # renderer is in a corrupted GL state (common after sandbox/module reloads).

        # Ensure renderer is healthy before scene reset/setup (deferred if needed)
        try:
            from EPCMRLib.Utilities.RendererRepairManager import RendererRepairManager
            from PyQt5 import QtCore

            def _run_or_defer_repair():
                lm = slicer.app.layoutManager()
                if lm:
                    try:
                        RendererRepairManager().repairAllRenderers()
                    except Exception:
                        pass
                else:
                    # layoutManager not ready yet; schedule a short deferred attempt
                    try:
                        QtCore.QTimer.singleShot(250, lambda: RendererRepairManager().repairAllRenderers())
                    except Exception:
                        # final fallback: best-effort immediate call
                        try:
                            RendererRepairManager().repairAllRenderers()
                        except Exception:
                            pass

            _run_or_defer_repair()
        except Exception:
            pass

        # --------------------------------------------------------------
        # 0) Revert catheter material properties and mapper lighting
        #    Ensures code parameter modifications are applied cleanly on module reloads.
        # --------------------------------------------------------------
        try:
            modelNodes = slicer.util.getNodesByClass("vtkMRMLModelNode")
            for node in modelNodes:
                nodeName = node.GetName()
                if nodeName and ("Abl" in nodeName or "Ref" in nodeName):
                    dn = node.GetDisplayNode()
                    if dn:
                        # Reset display parameters closer to standard default baselines
                        try:
                            dn.SetAmbient(0.1)
                        except Exception:
                            pass
                        try:
                            dn.SetDiffuse(0.9)
                        except Exception:
                            pass
                        try:
                            dn.SetSpecular(0.2)
                        except Exception:
                            pass

                    # Look up mapper to re-enable lighting so standard shaders apply
                    lm = slicer.app.layoutManager()
                    if lm:
                        threeDWidget = lm.threeDWidget(0)
                        if threeDWidget:
                            view = threeDWidget.threeDView()
                            if view:
                                renderer = view.renderWindow().GetRenderers().GetFirstRenderer()
                                if renderer:
                                    props = renderer.GetViewProps()
                                    props.InitTraversal()
                                    p = props.GetNextProp()
                                    while p:
                                        if hasattr(p, "GetMapper"):
                                            m = p.GetMapper()
                                            try:
                                                if m and m.GetInput() == node.GetPolyData():
                                                    # Restore default VTK cell-shading pipelines
                                                    try:
                                                        m.SetLighting(True)
                                                    except Exception:
                                                        pass
                                                    try:
                                                        m.ScalarVisibilityOn()
                                                    except Exception:
                                                        pass
                                                    break
                                            except Exception:
                                                # Some mapper implementations may raise on GetInput comparison
                                                pass
                                        p = props.GetNextProp()
        except Exception:
            # Reverting material pipelines must never stall cleanup
            pass

        # --------------------------------------------------------------
        # 1) Detach markups observers
        # --------------------------------------------------------------
        for key, tags in getattr(self, "_markupObserverTags", {}).items():
            node = getattr(self, "pNode", None)
            if node:
                targetNode = getattr(node, key, None)
                if targetNode:
                    for tag in tags:
                        try:
                            targetNode.RemoveObserver(tag)
                        except Exception:
                            pass
        self._markupObserverTags = {}

        # --------------------------------------------------------------
        # 2) Stop and drop backup timers
        # --------------------------------------------------------------
        for timer in getattr(self, "_saveTimers", {}).values():
            try:
                timer.stop()
            except Exception:
                pass
        self._saveTimers = {}

        # --------------------------------------------------------------
        # 3) Remove activation + voltage scalar bars from renderer
        #    SceneManager is the sole owner of these actors.
        # --------------------------------------------------------------
        renderer = self._getRenderer()

        if renderer and self.activationScalarBarActor:
            try:
                renderer.RemoveActor2D(self.activationScalarBarActor)
            except Exception:
                pass
        self.activationScalarBarActor = None
        self.activationScalarBarWidget = None

        if renderer and self.voltageScalarBarActor:
            try:
                renderer.RemoveActor2D(self.voltageScalarBarActor)
            except Exception:
                pass
        self.voltageScalarBarActor = None
        self.voltageScalarBarWidget = None

        # --------------------------------------------------------------
        # 4) Remove SceneManager-installed lights (no accumulation)
        # --------------------------------------------------------------
        try:
            # Delegate to LightsManager if available; ensures deterministic teardown
            if hasattr(self, "lightsManager") and self.lightsManager:
                try:
                    # Attempt a lightweight renderer repair immediately before lights cleanup
                    # to avoid removing lights from a corrupted renderer state.
                    if not getattr(self, "_lightsCleanupRepairAttempted", False):
                        try:
                            from EPCMRLib.Utilities.RendererRepairManager import RendererRepairManager

                            try:
                                RendererRepairManager().repairAllRenderers()
                            except Exception:
                                pass
                        except Exception:
                            pass
                        self._lightsCleanupRepairAttempted = True
                except Exception:
                    pass
                try:
                    self.lightsManager.cleanup()
                except Exception:
                    # Lighting cleanup must never break teardown
                    pass
        except Exception:
            # Lighting cleanup must never break teardown
            pass

        # --------------------------------------------------------------
        # 5) Remove SceneManager-generated catheter glow sheaths
        #    Ensures no phantom geometry tracks persist in data reload loops.
        # --------------------------------------------------------------
        try:
            # Safely fetch all model nodes currently allocated inside the active scene
            modelNodes = slicer.util.getNodesByClass("vtkMRMLModelNode")
            for node in modelNodes:
                nodeName = node.GetName()
                if nodeName and nodeName.endswith("_Glow_Rim_Sheath"):
                    try:
                        slicer.mrmlScene.RemoveNode(node)
                    except Exception:
                        pass
        except Exception:
            # Node teardown must never crash the main cleanup stack
            pass

        # --------------------------------------------------------------
        # 6) Re-apply material defaults to visible anatomy to ensure consistent lighting after cleanup
        # --------------------------------------------------------------
        try:
            # If SceneManager provides a centralized helper, use it; otherwise apply safe defaults inline.
            for node in slicer.util.getNodesByClass("vtkMRMLModelNode"):
                try:
                    name = (node.GetName() or "").lower()
                except Exception:
                    name = ""
                # Only re-apply to recognized anatomy nodes to avoid touching unrelated models
                try:
                    anatomy_keywords = []
                    for entry in getattr(self, "ANATOMY_MAP", {}).values():
                        anatomy_keywords.extend([k.lower() for k in entry.get("keywords", [])])
                except Exception:
                    anatomy_keywords = []

                if any(k in name for k in anatomy_keywords):
                    try:
                        # Prefer SceneManager helper if available
                        if hasattr(self, "applyMaterialDefaultsToNode"):
                            try:
                                # preserve existing opacity where anatomy intentionally set it
                                self.applyMaterialDefaultsToNode(node, preserveOpacity=True)
                            except Exception:
                                # fallback to inline defaults if helper fails
                                dn = node.GetDisplayNode()
                                if dn:
                                    try:
                                        dn.SetAmbient(0.02)
                                    except Exception:
                                        pass
                                    try:
                                        dn.SetDiffuse(0.95)
                                    except Exception:
                                        pass
                                    try:
                                        dn.SetSpecular(0.03)
                                    except Exception:
                                        pass
                                    try:
                                        dn.SetPower(10.0)
                                    except Exception:
                                        pass
                                    try:
                                        dn.SetScalarVisibility(0)
                                    except Exception:
                                        pass
                        else:
                            dn = node.GetDisplayNode()
                            if dn:
                                try:
                                    dn.SetAmbient(0.02)
                                except Exception:
                                    pass
                                try:
                                    dn.SetDiffuse(0.95)
                                except Exception:
                                    pass
                                try:
                                    dn.SetSpecular(0.03)
                                except Exception:
                                    pass
                                try:
                                    dn.SetPower(10.0)
                                except Exception:
                                    pass
                                try:
                                    dn.SetScalarVisibility(0)
                                except Exception:
                                    pass
                    except Exception:
                        # Per-node material application must not break cleanup
                        pass
        except Exception:
            pass

        # --------------------------------------------------------------
        # 7) Final render pass to ensure scene is consistent after cleanup
        # --------------------------------------------------------------
        try:
            slicer.util.forceRenderAllViews()
        except Exception:
            pass

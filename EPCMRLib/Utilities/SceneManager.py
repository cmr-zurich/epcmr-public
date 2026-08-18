# ruff: isort: skip_file
import os
import time
import logging

import qt
import slicer
import vtk

from EPCMRLib.EPCMRParameterNode import EPCMRParameterNode
from EPCMRLib.Utilities.LightsManager import LightsManager

VTK_VERSION = (vtk.vtkVersion.GetVTKMajorVersion(), vtk.vtkVersion.GetVTKMinorVersion())


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

        self.rimGlowEnabled = False

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

    # Strong Preset
    def boostRimGlow(self, modelNode):
        dn = modelNode.GetDisplayNode()
        if not dn:
            return

        dn.SetBackfaceCulling(False)
        dn.SetAmbient(0.25)
        dn.SetDiffuse(0.95)
        dn.SetSpecular(0.35)
        dn.SetPower(30)
        dn.SetOpacity(0.75)

    # Reset preset (neutral Slicer defaults)
    def resetRimGlow(self, modelNode):
        dn = modelNode.GetDisplayNode()
        if not dn:
            return

        dn.SetBackfaceCulling(False)
        dn.SetAmbient(0.10)
        dn.SetDiffuse(0.90)
        dn.SetSpecular(0.10)
        dn.SetPower(10)
        # Do NOT touch opacity here — leave anatomy opacity unchanged

    def applyRimGlowToAnatomy(self):
        keywords = []
        for entry in self.ANATOMY_MAP.values():
            keywords.extend([k.lower() for k in entry["keywords"]])

        for node in slicer.util.getNodesByClass("vtkMRMLModelNode"):
            name = node.GetName().lower()
            if any(k in name for k in keywords):
                self.boostRimGlow(node)

    def resetRimGlowOnAnatomy(self):
        keywords = []
        for entry in self.ANATOMY_MAP.values():
            keywords.extend([k.lower() for k in entry["keywords"]])

        for node in slicer.util.getNodesByClass("vtkMRMLModelNode"):
            name = node.GetName().lower()
            if any(k in name for k in keywords):
                self.resetRimGlow(node)

    def toggleRimGlow(self):
        if self.rimGlowEnabled:
            self.resetRimGlowOnAnatomy()
            self.rimGlowEnabled = False
            print("EPCMR: Rim glow disabled.")
        else:
            self.applyRimGlowToAnatomy()
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
        dn.SetScalarVisibility(False)
        dn.SetLighting(False)  # Turn off scene lighting equations completely

        # === REAL-TIME CARTO LIGHTING: Pure Emissive Performance ===
        dn.SetAmbient(1.00)  # 100% Self-illumination creates a brilliant, vibrant neon baseline
        dn.SetDiffuse(0.00)  # Completely remove directional shadows (prevents dirty/muddy undersides)
        dn.SetSpecular(0.00)  # Remove harsh reflections to keep the color purely saturated
        dn.SetPower(1)

        # === ELECTRODE SEAMS: High-speed edge definition ===
        dn.SetEdgeVisibility(True)
        dn.SetEdgeColor(0.10, 0.10, 0.10)  # Sharp dark seams distinctly separate electrode bands
        dn.SetLineWidth(1.5)  # Crisp, lightweight separation lines

        dn.SetBackfaceCulling(False)

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
                            if m and m.GetInput() == modelNode.GetPolyData():
                                m.SetLighting(False)  # Force drop lighting calculations on the GPU mapper
                                m.SetColorModeToDirectScalars()
                                m.ScalarVisibilityOff()  # Wipe out lighting-enforced scalar array paths
                        p = props.GetNextProp()
        except Exception:
            pass

        if is_vtk_at_least(9, 3):
            try:
                dn.SetInterpolationToPhong()
            except AttributeError:
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

                dn.SetScalarVisibility(False)
                dn.SetColor(*config["color"])
                dn.SetOpacity(0.6)
                dn.SetAmbient(0.15)
                dn.SetBackfaceCulling(False)
                dn.SetVisibility(True)

                attr = config["attr"]
                if attr:
                    setattr(self.pNode, attr, modelNode)

                if hasattr(self, "normalizeAnatomyAppearance"):
                    self.normalizeAnatomyAppearance(modelNode)
                if getattr(self, "rimGlowEnabled", False):
                    self.boostRimGlow(modelNode)

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

        dn.SetAmbient(0.45)
        dn.SetDiffuse(0.85)
        dn.SetSpecular(0.10)
        dn.SetPower(10)
        dn.SetOpacity(0.60)

        dn.SetLighting(True)
        dn.SetShading(True)

        try:
            if hasattr(dn, "SetInterpolationToGouraud"):
                dn.SetInterpolationToGouraud()
        except AttributeError:
            pass

        polyData = modelNode.GetPolyData()
        if not polyData:
            return

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
          - Skip nodes already marked with display node attribute "EPCMR.RimGlowApplied" == "true".
          - Call self.boostRimGlow(node, color=...) if color is provided, otherwise self.boostRimGlow(node).
          - Return list of affected node names.
        """
        import logging
        import re

        logger = getattr(self, "logger", logging)

        # Build canonical -> alias list mapping from ANATOMY_MAP
        token_map = {}  # canonical_key -> dict { "aliases": [...], "color": (r,g,b) or None, "attr": attrname or None }
        anatomy_map = getattr(self, "ANATOMY_MAP", None)

        try:
            if isinstance(anatomy_map, dict):
                for canonical, entry in anatomy_map.items():
                    try:
                        aliases = []
                        color = None
                        attr = None
                        if isinstance(entry, dict):
                            aliases = [str(k).strip().lower() for k in entry.get("keywords", []) if k]
                            color = entry.get("color", None)
                            attr = entry.get("attr", None)
                        else:
                            # entry might be a simple token or list
                            if isinstance(entry, (list, tuple, set)):
                                aliases = [str(x).strip().lower() for x in entry if x]
                            else:
                                aliases = [str(entry).strip().lower()]
                        # always include canonical key itself
                        if canonical:
                            aliases.insert(0, str(canonical).strip().lower())
                        # dedupe while preserving order
                        seen = set()
                        aliases = [a for a in aliases if a and not (a in seen or seen.add(a))]
                        token_map[str(canonical)] = {"aliases": aliases, "color": color, "attr": attr}
                    except Exception:
                        # skip malformed entry but continue
                        logger.debug("boostRimGlowOnAllAnatomy: malformed ANATOMY_MAP entry for %s", str(canonical))
                        continue
            elif anatomy_map:
                # treat as iterable of tokens
                for entry in anatomy_map:
                    e = str(entry).strip()
                    if e:
                        token_map[e] = {"aliases": [e.lower()], "color": None, "attr": None}
            else:
                # fallback minimal map
                token_map = {
                    "SVC": {"aliases": ["svc"], "color": None, "attr": None},
                    "IVC": {"aliases": ["ivc"], "color": None, "attr": None},
                }
        except Exception:
            token_map = {
                "SVC": {"aliases": ["svc"], "color": None, "attr": None},
                "IVC": {"aliases": ["ivc"], "color": None, "attr": None},
            }

        # Helper: normalize a model name into searchable tokens
        def _tokenize_name(name):
            if not name:
                return []
            s = re.sub(r"[^a-z0-9]+", " ", name.lower())
            tokens = [t for t in s.split() if t]
            return tokens

        # Helper: resolve attr -> node(s)
        def _resolve_attr_node(attrname):
            """
            Try to resolve an attribute name on self to a node object or node name/ID.
            Returns a list of vtkMRMLNode objects (possibly empty).
            """
            resolved = []
            if not attrname:
                return resolved
            try:
                if hasattr(self, attrname):
                    val = getattr(self, attrname)
                    # If it's already a node
                    try:
                        if val and hasattr(val, "IsA") and val.IsA("vtkMRMLNode"):
                            resolved.append(val)
                            return resolved
                    except Exception:
                        pass
                    # If it's a string, try to find node by name or id
                    try:
                        sval = str(val)
                        node = slicer.util.getFirstNodeByName(sval)
                        if node:
                            resolved.append(node)
                            return resolved
                        # try by id
                        node = slicer.mrmlScene.GetNodeByID(sval)
                        if node:
                            resolved.append(node)
                            return resolved
                    except Exception:
                        pass
            except Exception:
                pass
            return resolved

        # Collect all model nodes once
        try:
            model_nodes = list(slicer.util.getNodesByClass("vtkMRMLModelNode"))
        except Exception:
            model_nodes = []
            try:
                it = slicer.mrmlScene.GetNodes()
                it.InitTraversal()
                for i in range(it.GetNumberOfItems()):
                    n = it.GetItemAsObject(i)
                    try:
                        if n.IsA("vtkMRMLModelNode"):
                            model_nodes.append(n)
                    except Exception:
                        continue
            except Exception:
                model_nodes = []

        affected = []
        applied_nodes = set()  # track node IDs to avoid duplicates

        # First, include nodes referenced by attrs (explicit mapping)
        for canonical, meta in token_map.items():
            attrname = meta.get("attr", None)
            if attrname:
                try:
                    resolved = _resolve_attr_node(attrname)
                    for node in resolved:
                        try:
                            nid = node.GetID()
                            if nid in applied_nodes:
                                continue
                            # ensure display node exists
                            dn = node.GetDisplayNode()
                            if not dn:
                                try:
                                    node.CreateDefaultDisplayNodes()
                                    dn = node.GetDisplayNode()
                                except Exception:
                                    logger.warning(
                                        "boostRimGlowOnAllAnatomy: could not create display node for %s", node.GetName()
                                    )
                                    continue
                            # skip if already tagged
                            try:
                                if hasattr(dn, "GetAttribute"):
                                    tag = dn.GetAttribute("EPCMR.RimGlowApplied")
                                    if tag and str(tag).lower() == "true":
                                        continue
                            except Exception:
                                pass
                            # apply
                            try:
                                color = meta.get("color", None)
                                if color is not None:
                                    try:
                                        self.boostRimGlow(node, color=color)
                                    except TypeError:
                                        # fallback if boostRimGlow doesn't accept color kwarg
                                        self.boostRimGlow(node)
                                else:
                                    self.boostRimGlow(node)
                                if hasattr(dn, "SetAttribute"):
                                    dn.SetAttribute("EPCMR.RimGlowApplied", "true")
                                affected.append(node.GetName())
                                applied_nodes.add(nid)
                                logger.info(
                                    "boostRimGlowOnAllAnatomy: applied rim-glow to %s via attr %s",
                                    node.GetName(),
                                    attrname,
                                )
                            except Exception as e:
                                logger.exception(
                                    "boostRimGlowOnAllAnatomy: failed to apply rim-glow to %s: %s",
                                    node.GetName(),
                                    str(e),
                                )
                        except Exception:
                            continue
                except Exception:
                    continue

        # Now match by keywords against model names
        for node in model_nodes:
            try:
                nid = node.GetID()
                if nid in applied_nodes:
                    continue
                name = (node.GetName() or "").strip()
            except Exception:
                continue
            if not name:
                continue
            lname = name.lower()
            name_tokens = _tokenize_name(name)

            matched = False
            matched_meta = None
            matched_alias = None

            for canonical, meta in token_map.items():
                aliases = meta.get("aliases", []) or []
                for alias in aliases:
                    if not alias:
                        continue
                    # token equality match
                    if alias in name_tokens:
                        matched = True
                        matched_meta = meta
                        matched_alias = alias
                        break
                    # substring fallback
                    if alias in lname:
                        matched = True
                        matched_meta = meta
                        matched_alias = alias
                        break
                if matched:
                    break

            if not matched:
                continue

            # Ensure display node exists
            dn = node.GetDisplayNode()
            if not dn:
                try:
                    node.CreateDefaultDisplayNodes()
                    dn = node.GetDisplayNode()
                except Exception:
                    logger.warning("boostRimGlowOnAllAnatomy: could not create display node for %s", name)
                    continue

            # Skip if already applied
            try:
                if hasattr(dn, "GetAttribute"):
                    tag = dn.GetAttribute("EPCMR.RimGlowApplied")
                    if tag and str(tag).lower() == "true":
                        logger.debug("boostRimGlowOnAllAnatomy: skipping %s (already tagged)", name)
                        applied_nodes.add(nid)
                        continue
            except Exception:
                pass

            # Apply rim glow
            try:
                color = matched_meta.get("color", None) if matched_meta else None
                if color is not None:
                    try:
                        self.boostRimGlow(node, color=color)
                    except TypeError:
                        self.boostRimGlow(node)
                else:
                    self.boostRimGlow(node)
                try:
                    if hasattr(dn, "SetAttribute"):
                        dn.SetAttribute("EPCMR.RimGlowApplied", "true")
                except Exception:
                    pass
                affected.append(name)
                applied_nodes.add(nid)
                logger.info("boostRimGlowOnAllAnatomy: applied rim-glow to %s (matched %s)", name, matched_alias)
            except Exception as e:
                logger.exception("boostRimGlowOnAllAnatomy: failed to apply rim-glow to %s: %s", name, str(e))
                continue

        # Force a render so changes are visible immediately
        try:
            slicer.util.forceRenderAllViews()
        except Exception:
            pass

        return affected

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

        from .SceneManager import is_vtk_at_least  # or import at top if same file

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
                        dn.SetAmbient(0.1)
                        dn.SetDiffuse(0.9)
                        dn.SetSpecular(0.2)

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
                                            if m and m.GetInput() == node.GetPolyData():
                                                # Restore default VTK cell-shading pipelines
                                                m.SetLighting(True)
                                                m.ScalarVisibilityOn()
                                                break
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
                self.lightsManager.cleanup()
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

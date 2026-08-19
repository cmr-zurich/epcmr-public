# ruff: isort: skip_file
import logging
import slicer
import vtk
import re

from EPCMRLib.Utilities.LightsManager import LightsManager

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


class MaterialManager:
    """
    Dedicated manager for rim glow, rim overlays, and anatomy material presets.

    Responsibilities:
      - Apply rim glow presets (idempotent)
      - Apply/remove rim overlays (idempotent)
      - Reset anatomy materials to neutral defaults
      - Full restore of anatomy appearance
      - Provide clean delegation for SceneManager
      - Execute all rim-glow and material operations that SceneManager orchestrates
        (SceneManager decides *which* nodes; MaterialManager performs the work)
    """

    def __init__(self, sceneManager):
        """
        MaterialManager receives SceneManager so it can access:
          - ANATOMY_MAP
          - lightsManager
          - normalizeAnatomyAppearance()
        """
        self.sceneManager = sceneManager
        self.logger = getattr(sceneManager, "logger", logging)

    # ------------------------------------------------------------------
    # Rim Glow Preset (material only)
    # ------------------------------------------------------------------
    def applyRimGlow(self, modelNode, color=None):
        """
        CARTO-style rim glow: subtle, warm, tight Fresnel, non-destructive.
        Idempotent: safe to call repeatedly.
        """
        if not modelNode:
            return

        modelNode.CreateDefaultDisplayNodes()
        dn = modelNode.GetDisplayNode()
        if not dn:
            return

        # --- CARTO baseline lighting ---
        dn.SetLighting(True)
        dn.SetShading(True)

        # CARTO keeps ambient low and diffuse high
        dn.SetAmbient(0.06)
        dn.SetDiffuse(0.88)

        # Specular almost off
        dn.SetSpecular(0.04)
        try:
            dn.SetPower(8)
        except AttributeError:
            pass

        # --- Fresnel rim parameters (CARTO-style) ---
        rimIntensity = 0.12  # subtle
        rimPower = 2.4  # tight falloff
        rimColor = color if color else (1.0, 0.65, 0.45)  # warm CARTO tone

        # Store parameters for shader callback
        modelNode.SetAttribute("EPCMR_RimIntensity", str(rimIntensity))
        modelNode.SetAttribute("EPCMR_RimPower", str(rimPower))
        modelNode.SetAttribute("EPCMR_RimColorR", str(rimColor[0]))
        modelNode.SetAttribute("EPCMR_RimColorG", str(rimColor[1]))
        modelNode.SetAttribute("EPCMR_RimColorB", str(rimColor[2]))

        # Mark node
        try:
            dn.SetAttribute("EPCMR.RimGlowApplied", "true")
        except Exception:
            pass

        modelNode.Modified()

    # ------------------------------------------------------------------
    # Rim Overlay (idempotent)
    # ------------------------------------------------------------------
    def applyRimOverlay(self, modelNode, overlaySuffix="_rimOverlay"):
        """
        CARTO-style overlay: extremely subtle, only enhances edges.
        Idempotent: safe to call repeatedly.
        """
        if not modelNode:
            return None

        baseName = modelNode.GetName() or "Anatomy"
        overlayName = f"{baseName}{overlaySuffix}"

        # 1. Try to find existing overlay
        overlay = slicer.util.getFirstNodeByName(overlayName)
        if not overlay or not overlay.IsA("vtkMRMLModelNode"):
            overlay = slicer.mrmlScene.AddNewNodeByClass("vtkMRMLModelNode", overlayName)

        # 2. Sync geometry
        try:
            basePD = modelNode.GetPolyData()
            if basePD:
                pd = vtk.vtkPolyData()
                pd.ShallowCopy(basePD)
                overlay.SetAndObservePolyData(pd)
        except Exception:
            pass

        # 3. Ensure display node exists
        overlay.CreateDefaultDisplayNodes()
        dn = overlay.GetDisplayNode()
        if not dn:
            return overlay

        # --- CARTO-style overlay (very subtle) ---
        dn.SetLighting(True)
        dn.SetShading(True)

        dn.SetOpacity(0.05)  # extremely subtle
        dn.SetAmbient(0.10)
        dn.SetDiffuse(0.20)
        dn.SetSpecular(0.05)
        dn.SetPower(10.0)

        dn.SetBackfaceCulling(False)
        dn.SetEdgeVisibility(False)

        # 4. Follow base transform
        try:
            overlay.SetAndObserveTransformNodeID(modelNode.GetTransformNodeID())
        except Exception:
            pass

        # 5. Do not save overlay with scene
        try:
            overlay.SetSaveWithScene(False)
        except Exception:
            pass

        return overlay

    # ------------------------------------------------------------------
    # Remove Rim Overlay (idempotent)
    # ------------------------------------------------------------------
    def removeRimOverlay(self, modelNode, overlaySuffix="_rimOverlay"):
        """
        Idempotent removal of rim overlay:
          - Finds overlay node by name.
          - Removes it safely from the scene.
          - Does nothing if overlay does not exist.
        """
        if not modelNode:
            return

        baseName = modelNode.GetName() or "Anatomy"
        overlayName = f"{baseName}{overlaySuffix}"

        overlay = slicer.util.getFirstNodeByName(overlayName)
        if overlay and overlay.IsA("vtkMRMLModelNode"):
            try:
                slicer.mrmlScene.RemoveNode(overlay)
            except Exception:
                pass

    # ------------------------------------------------------------------
    # Reset Rim Material (neutral defaults)
    # ------------------------------------------------------------------
    def resetRimMaterial(self, modelNode):
        """
        Restore default Slicer material for a modelNode.
        Removes rim-glow material settings.
        """
        if not modelNode:
            return

        dn = modelNode.GetDisplayNode()
        if not dn:
            try:
                modelNode.CreateDefaultDisplayNodes()
                dn = modelNode.GetDisplayNode()
            except Exception:
                return

        try:
            # Restore default Slicer lighting
            dn.SetLighting(True)
            dn.SetShading(True)

            # Default-ish Slicer material values
            dn.SetAmbient(0.1)
            dn.SetDiffuse(0.9)
            dn.SetSpecular(0.1)
            dn.SetPower(10.0)

            dn.SetBackfaceCulling(False)
            dn.SetEdgeVisibility(False)

            # Remove rim tag
            if hasattr(dn, "SetAttribute"):
                dn.SetAttribute("EPCMR.RimGlowApplied", "false")

        except Exception:
            pass

    # ------------------------------------------------------------------
    # Helper: tokenize model names
    # ------------------------------------------------------------------
    def _tokenize(self, name):
        if not name:
            return []
        s = re.sub(r"[^a-z0-9]+", " ", name.lower())
        return [t for t in s.split() if t]

    # ------------------------------------------------------------------
    # Apply Rim Glow to All Anatomy
    # ------------------------------------------------------------------
    def boostRimGlowOnAllAnatomy(self):
        """
        Delegated version of SceneManager.boostRimGlowOnAllAnatomy.
        Uses ANATOMY_MAP from SceneManager.
        """
        sm = self.sceneManager
        anatomy_map = getattr(sm, "ANATOMY_MAP", {})
        logger = self.logger

        # Build canonical -> alias list mapping
        token_map = {}
        try:
            for canonical, entry in anatomy_map.items():
                aliases = []
                color = None
                attr = None

                if isinstance(entry, dict):
                    aliases = [str(k).strip().lower() for k in entry.get("keywords", []) if k]
                    color = entry.get("color", None)
                    attr = entry.get("attr", None)
                else:
                    if isinstance(entry, (list, tuple, set)):
                        aliases = [str(x).strip().lower() for x in entry if x]
                    else:
                        aliases = [str(entry).strip().lower()]

                if canonical:
                    aliases.insert(0, canonical.lower())

                seen = set()
                aliases = [a for a in aliases if a and not (a in seen or seen.add(a))]

                token_map[canonical] = {"aliases": aliases, "color": color, "attr": attr}
        except Exception:
            token_map = {}

        # Collect model nodes
        try:
            model_nodes = list(slicer.util.getNodesByClass("vtkMRMLModelNode"))
        except Exception:
            model_nodes = []

        affected = []
        applied_ids = set()

        # First: explicit attr nodes
        for canonical, meta in token_map.items():
            attr = meta.get("attr")
            if not attr:
                continue

            try:
                val = getattr(sm.pNode, attr, None)
            except Exception:
                continue

            nodes = []
            if val and hasattr(val, "IsA") and val.IsA("vtkMRMLNode"):
                nodes.append(val)
            else:
                try:
                    sval = str(val)
                    node = slicer.util.getFirstNodeByName(sval)
                    if node:
                        nodes.append(node)
                    node = slicer.mrmlScene.GetNodeByID(sval)
                    if node:
                        nodes.append(node)
                except Exception:
                    pass

            for node in nodes:
                try:
                    nid = node.GetID()
                    if nid in applied_ids:
                        continue

                    # Material
                    color = meta.get("color")
                    self.applyRimGlow(node, color=color)

                    # Overlay
                    self.applyRimOverlay(node)

                    applied_ids.add(nid)
                    affected.append(node.GetName())
                except Exception:
                    continue

        # Second: keyword matching
        for node in model_nodes:
            try:
                nid = node.GetID()
                if nid in applied_ids:
                    continue

                name = node.GetName() or ""
                lname = name.lower()
                tokens = self._tokenize(name)
            except Exception:
                continue

            matched_meta = None
            for canonical, meta in token_map.items():
                for alias in meta["aliases"]:
                    if alias in tokens or alias in lname:
                        matched_meta = meta
                        break
                if matched_meta:
                    break

            if not matched_meta:
                continue

            try:
                color = matched_meta.get("color")
                self.applyRimGlow(node, color=color)
                self.applyRimOverlay(node)

                applied_ids.add(nid)
                affected.append(name)
            except Exception:
                continue

        try:
            slicer.util.forceRenderAllViews()
        except Exception:
            pass

        return affected

    # ------------------------------------------------------------------
    # Reset Rim Glow on All Anatomy
    # ------------------------------------------------------------------
    def resetRimGlowOnAllAnatomy(self):
        """
        Reverse of boostRimGlowOnAllAnatomy:
          - Removes rim material.
          - Removes rim overlay.
          - Clears rim-glow tag.
        """
        sm = self.sceneManager
        anatomy_map = getattr(sm, "ANATOMY_MAP", {})
        logger = self.logger

        # Build alias map
        token_map = {}
        try:
            for canonical, entry in anatomy_map.items():
                aliases = []
                if isinstance(entry, dict):
                    aliases = [str(k).strip().lower() for k in entry.get("keywords", []) if k]
                else:
                    if isinstance(entry, (list, tuple, set)):
                        aliases = [str(x).strip().lower() for x in entry if x]
                    else:
                        aliases = [str(entry).strip().lower()]

                if canonical:
                    aliases.insert(0, canonical.lower())

                seen = set()
                aliases = [a for a in aliases if a and not (a in seen or seen.add(a))]

                token_map[canonical] = {"aliases": aliases}
        except Exception:
            token_map = {}

        # Collect model nodes
        try:
            model_nodes = list(slicer.util.getNodesByClass("vtkMRMLModelNode"))
        except Exception:
            model_nodes = []

        affected = []

        for node in model_nodes:
            try:
                name = node.GetName() or ""
                lname = name.lower()
                tokens = self._tokenize(name)
            except Exception:
                continue

            matched = False
            for canonical, meta in token_map.items():
                for alias in meta["aliases"]:
                    if alias in tokens or alias in lname:
                        matched = True
                        break
                if matched:
                    break

            if not matched:
                continue

            # Remove overlay
            self.removeRimOverlay(node)

            # Reset material
            self.resetRimMaterial(node)

            affected.append(name)

        try:
            slicer.util.forceRenderAllViews()
        except Exception:
            pass

        return affected

    # ------------------------------------------------------------------
    # Full Restore
    # ------------------------------------------------------------------
    def restoreAllAnatomyAppearance(self):
        """
        Full restore:
          - Removes rim glow material.
          - Removes rim overlays.
          - Clears rim tags.
          - Restores default lighting via lightsManager.
          - Normalizes anatomy appearance.
        """
        sm = self.sceneManager

        try:
            affected = self.resetRimGlowOnAllAnatomy()
        except Exception:
            affected = []

        # Restore EPCMR lighting rig
        try:
            sm.lightsManager.resetLighting()
        except Exception:
            pass

        # Normalize anatomy appearance
        try:
            sm.normalizeAllAnatomyAppearance()
        except Exception:
            pass

        try:
            slicer.util.forceRenderAllViews()
        except Exception:
            pass

        return affected

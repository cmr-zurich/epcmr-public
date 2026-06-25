# ruff: isort: skip_file
import slicer
import vtk
import logging


class RACloneManager:
    """
    Owns RA + cloned RA geometry and visibility state.

    RA (anatomical model) is always a NON-cloned node:
      - pNode.raModel
      - "RightAtrium"
      - "RightAtriumCardiac_Model"

    Clone is always:
      - pNode.raClonedModel
      - "RightAtrium_Cloned"

    Neutral state semantics:
      - 0 mapping points -> cyan anatomical RA, clone hidden
      - 1 mapping point -> bluish neutral clone, RA hidden
      - >=2 mapping points -> heatmap mode (handled externally)
    """

    RA_COLOR = (10 / 255, 200 / 255, 205 / 255)  # cyan baseline
    NEUTRAL_CLONE_COLOR = (0.40, 0.45, 0.55)  # CARTO-style bluish neutral
    NEUTRAL_OPACITY = 0.60

    def __init__(self, pNode):
        self.pNode = pNode
        self.RA = None
        self.clonedRA = None

        # Initial lazy resolution (safe during startup)
        self._refresh_ra()
        self._refresh_clone()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------
    def _refresh_ra(self):
        """
        Ensure self.RA points to a valid, NON-cloned RA model node.

        The anatomical RA model's name MUST start with "RightAtriumCardiac"
        (e.g. "RightAtriumCardiac BJ 78465309").
        Any such node is treated as the authoritative RA geometry.
        """
        # 1) Prefer existing pNode.raModel if still valid and non-cloned
        ra = getattr(self.pNode, "raModel", None)
        if ra and slicer.mrmlScene.GetNodeByID(ra.GetID()):
            name = ra.GetName() or ""
            if name.startswith("RightAtriumCardiac") and "Cloned" not in name:
                self.RA = ra
                return self.RA

        # 2) Fallback: search all model nodes whose name starts with "RightAtriumCardiac"
        for m in slicer.util.getNodesByClass("vtkMRMLModelNode"):
            name = m.GetName() or ""
            if name.startswith("RightAtriumCardiac") and "Cloned" not in name:
                self.RA = m
                self.pNode.raModel = m
                return self.RA

        # 3) Legacy exact-name fallbacks
        for name in ["RightAtrium", "RightAtriumCardiac_Model"]:
            ra = slicer.util.getFirstNodeByName(name)
            if ra and "Cloned" not in ra.GetName():
                self.RA = ra
                self.pNode.raModel = ra
                return self.RA

        self.RA = None
        return None

    def _refresh_clone(self):
        """
        Ensure self.clonedRA points to a valid cloned RA node.
        """
        clone = getattr(self.pNode, "raClonedModel", None)
        if clone and slicer.mrmlScene.GetNodeByID(clone.GetID()):
            if "Cloned" in clone.GetName():
                self.clonedRA = clone
                return self.clonedRA

        clone = slicer.util.getFirstNodeByName("RightAtrium_Cloned")
        if clone:
            self.clonedRA = clone
            self.pNode.raClonedModel = clone
            return self.clonedRA

        self.clonedRA = None
        return None

    # ------------------------------------------------------------------
    # Clone lifecycle
    # ------------------------------------------------------------------
    def ensure_clone(self):
        """
        Lazily ensures the cloned RA exists and has:
          - its OWN PolyData (deep copy)
          - its OWN display node
          - never shares geometry or display nodes with RA

        Safe during module startup: if RA has no geometry yet,
        simply return the existing clone (or None) without error.
        """

        # If clone already exists and has valid geometry -> return it
        if self.clonedRA and self.clonedRA.GetScene():
            poly = self.clonedRA.GetPolyData()
            if poly and poly.GetNumberOfPoints() > 0:
                return self.clonedRA

        # Refresh RA reference
        ra = self._refresh_ra()

        # If RA not ready yet -> do NOT clone now
        if not ra or not ra.GetPolyData() or ra.GetPolyData().GetNumberOfPoints() == 0:
            return self.clonedRA

        # --- Create clone model node ---
        self.clonedRA = slicer.mrmlScene.AddNewNodeByClass(
            "vtkMRMLModelNode",
            "RightAtrium_Cloned",
        )

        # --- Deep copy geometry ---
        poly_copy = vtk.vtkPolyData()
        poly_copy.DeepCopy(ra.GetPolyData())
        self.clonedRA.SetAndObservePolyData(poly_copy)

        # --- Create independent display node ---
        dn = slicer.mrmlScene.AddNewNodeByClass("vtkMRMLModelDisplayNode")

        # NEVER use LUT range; always use data range
        dn.SetScalarRangeFlag(0)

        # CARTO-style bluish neutral clone color (1-point mode)
        dn.SetColor(*self.NEUTRAL_CLONE_COLOR)
        dn.SetOpacity(self.NEUTRAL_OPACITY)
        dn.SetScalarVisibility(False)

        self.clonedRA.SetAndObserveDisplayNodeID(dn.GetID())
        self.pNode.raClonedModel = self.clonedRA

        return self.clonedRA

    # ------------------------------------------------------------------
    # Neutral / gray states
    # ------------------------------------------------------------------
    def set_neutral_state(self, n=None, num_points=None):
        """
        Neutral visualization state.

        Semantics:
            0 -> RA visible (cyan), clone hidden
            1 -> clone visible (bluish neutral), RA hidden
            >=2 -> handled by heatmap state (GeometryInterpolator)
        """

        # Resolve argument
        if num_points is not None:
            n = num_points
        if n is None:
            n = 0

        ra = self._refresh_ra()
        clone = self.ensure_clone()

        # ------------------------------------------------------------------
        # 0 POINTS -> ORIGINAL RA VISIBLE (CYAN), CLONE HIDDEN
        # ------------------------------------------------------------------
        if n == 0:
            if ra and ra.GetDisplayNode():
                dn = ra.GetDisplayNode()
                dn.SetVisibility(True)
                dn.SetScalarVisibility(False)
                dn.SetColor(*self.RA_COLOR)
                dn.SetOpacity(self.NEUTRAL_OPACITY)

            if clone and clone.GetDisplayNode():
                clone_dn = clone.GetDisplayNode()
                clone_dn.SetVisibility(False)
                clone_dn.SetScalarVisibility(False)

            slicer.util.forceRenderAllViews()
            return

        # ------------------------------------------------------------------
        # 1 POINT -> CLONE VISIBLE (BLUISH NEUTRAL), RA HIDDEN
        # ------------------------------------------------------------------
        if n == 1:
            if ra and ra.GetDisplayNode():
                ra.GetDisplayNode().SetVisibility(False)

            if clone and clone.GetDisplayNode():
                dn = clone.GetDisplayNode()
                dn.SetVisibility(True)
                dn.SetScalarVisibility(False)
                dn.SetColor(*self.NEUTRAL_CLONE_COLOR)
                dn.SetOpacity(self.NEUTRAL_OPACITY)

            slicer.util.forceRenderAllViews()
            return

        # ------------------------------------------------------------------
        # >=2 POINTS -> HEATMAP MODE (handled externally)
        # ------------------------------------------------------------------
        return

    # ------------------------------------------------------------------
    # Heatmap state (n >= 2)
    # ------------------------------------------------------------------
    def set_heatmap_state(self):
        """
        Heatmap visualization state for n >= 2 mapping points.

        Responsibilities:
          - Hide anatomical RA
          - Show cloned RA
          - Enable scalar visibility on clone
          - Do NOT set ActiveScalarName here
          - Do NOT set scalar range here
        """

        if getattr(self.pNode, "replayModeActive", False):
            return

        ra = self._refresh_ra()
        clone = self.ensure_clone()
        if not clone or not ra:
            return

        dn_clone = clone.GetDisplayNode()
        dn_ra = ra.GetDisplayNode()
        if not dn_clone or not dn_ra:
            return

        # Hide anatomical RA
        dn_ra.SetVisibility(False)

        # Show clone + enable scalar visibility
        dn_clone.SetVisibility(True)
        dn_clone.SetScalarVisibility(True)

        slicer.util.forceRenderAllViews()

# ruff: isort: skip_file
import vtk
import slicer


class ColorMapper:
    """
    Pure color-mapping helper.

    Responsibilities:
      - Apply activation LUT (externally provided procedural node, or lazy default)
      - Build and maintain voltage CTF + procedural color node
      - Apply LUTs/CTFs to clone display nodes
      - Never modify activation LUT CTF
      - Never modify clone geometry
      - Maintain an immutable base voltage CTF (CARTO-like)
    """

    def __init__(self, activation_color_node):
        """
        activation_color_node:
            Procedural color node (Sascha's Rainbow) created and owned by SceneManager.
            May be None; in that case a default Rainbow LUT is created lazily.
        """
        self.activationColorNode = activation_color_node

        # ------------------------------------------------------------------
        # IMMUTABLE BASE VOLTAGE CTF (CARTO-like, 0-3 mV)
        # ------------------------------------------------------------------
        self.baseVoltageCTF = vtk.vtkDiscretizableColorTransferFunction()
        self.baseVoltageCTF.SetColorSpaceToRGB()
        self.baseVoltageCTF.SetScaleToLinear()
        self.baseVoltageCTF.SetNumberOfValues(128)
        self.baseVoltageCTF.DiscretizeOff()
        self.baseVoltageCTF.RemoveAllPoints()

        # CARTO-like bipolar voltage map (0-3 mV)
        # Dense scar band (0.00-0.10 mV) -> flat red
        self.baseVoltageCTF.AddRGBPoint(0.00, 1.00, 0.00, 0.00)  # red
        self.baseVoltageCTF.AddRGBPoint(0.10, 1.00, 0.00, 0.00)  # red

        # Border zone (yellow -> green)
        self.baseVoltageCTF.AddRGBPoint(0.50, 1.00, 0.50, 0.00)  # orange
        self.baseVoltageCTF.AddRGBPoint(1.00, 1.00, 1.00, 0.00)  # yellow
        self.baseVoltageCTF.AddRGBPoint(1.50, 0.00, 1.00, 0.00)  # green

        # Healthy tissue (blue -> purple)
        self.baseVoltageCTF.AddRGBPoint(2.25, 0.00, 0.00, 1.00)  # blue
        self.baseVoltageCTF.AddRGBPoint(3.00, 0.60, 0.00, 0.80)  # purple

        self.baseVoltageCTF.SetRange(0.0, 3.0)

        # ------------------------------------------------------------------
        # WORKING VOLTAGE CTF (reset from base on each call)
        # ------------------------------------------------------------------
        self.voltageCTF = vtk.vtkDiscretizableColorTransferFunction()
        self.voltageCTF.DeepCopy(self.baseVoltageCTF)

        # ------------------------------------------------------------------
        # Voltage procedural color node (never shared with activation LUT)
        # ------------------------------------------------------------------
        self.voltageColorNode = slicer.mrmlScene.AddNewNodeByClass(
            "vtkMRMLProceduralColorNode",
            "EPCMR_VoltageLUT",
        )

        # High-resolution internal table for smooth gradients
        self.voltageColorNode.SetNumberOfTableValues(4096)

        # Attach the working CTF; all edits go via self.voltageCTF
        self.voltageColorNode.SetAndObserveColorTransferFunction(self.voltageCTF)

        self.voltageColorNode.SetType(slicer.vtkMRMLColorTableNode.User)
        self.voltageColorNode.SetHideFromEditors(False)
        self.voltageColorNode.Modified()

    # ----------------------------------------------------------------------
    # ACTIVATION COLORMAP
    # ----------------------------------------------------------------------
    def _ensure_activation_node(self):
        """
        Ensure self.activationColorNode is a valid MRML color node.

        If none was provided from SceneManager, create a default Rainbow LUT.
        """
        if self.activationColorNode and self.activationColorNode.GetScene():
            return self.activationColorNode

        node = slicer.mrmlScene.AddNewNodeByClass(
            "vtkMRMLColorTableNode",
            "EPCMR_ActivationLUT",
        )
        node.SetTypeToRainbow()
        self.activationColorNode = node
        return node

    def apply_activation_colormap(self, clone):
        """
        Apply activation-time colormap to the clone.

        Uses the externally provided activationColorNode if available,
        otherwise a lazily created Rainbow LUT.
        """
        if not clone:
            return

        dn = clone.GetDisplayNode()
        if not dn:
            return

        color_node = self._ensure_activation_node()
        dn.SetAndObserveColorNodeID(color_node.GetID())
        dn.SetScalarVisibility(True)

        try:
            dn.SetScalarRangeFlag(slicer.vtkMRMLDisplayNode.UseDataScalarRange)
        except Exception:
            pass

        dn.Modified()

    # ----------------------------------------------------------------------
    # VOLTAGE COLORMAP (CARTO-like, clamp-free)
    # ----------------------------------------------------------------------
    def apply_voltage_colormap(self, clone, lowCutoff, highCutoff):
        """
        Apply voltage colormap to the clone.

        Correct behavior:
          - Normalize CARTO palette to the voltage window [lowCutoff, highCutoff]
          - Red band anchored at lowCutoff
          - Purple/magenta anchored at highCutoff
          - -1.0 sentinel mapped to skin tone (unmapped vertices)
          - Display node uses COLOR NODE RANGE (never data range)
          - No dependence on actual data min/max
        """

        if not clone:
            return

        dn = clone.GetDisplayNode()
        if not dn:
            return

        # ------------------------------------------------------------------
        # Normalize voltage window and ensure strictly increasing range
        # ------------------------------------------------------------------
        low = float(lowCutoff)
        high = float(highCutoff)

        # Sort to avoid inverted windows
        low, high = sorted([low, high])

        # Avoid zero-width window
        if high == low:
            high = low + 1e-6

        # ------------------------------------------------------------------
        # Rebuild working CTF normalized to [low, high]
        # ------------------------------------------------------------------
        self.voltageCTF.RemoveAllPoints()
        span = high - low

        # Dense scar (flat red)
        self.voltageCTF.AddRGBPoint(low, 1.00, 0.00, 0.00)  # red
        self.voltageCTF.AddRGBPoint(low + 0.10 * span, 1.00, 0.00, 0.00)

        # Border zone (orange -> yellow -> green)
        self.voltageCTF.AddRGBPoint(low + 0.30 * span, 1.00, 0.50, 0.00)  # orange
        self.voltageCTF.AddRGBPoint(low + 0.50 * span, 1.00, 1.00, 0.00)  # yellow
        self.voltageCTF.AddRGBPoint(low + 0.70 * span, 0.00, 1.00, 0.00)  # green

        # Healthy tissue (blue -> purple/magenta)
        self.voltageCTF.AddRGBPoint(low + 0.90 * span, 0.00, 0.00, 1.00)  # blue
        self.voltageCTF.AddRGBPoint(high, 0.60, 0.00, 0.80)  # purple/magenta

        # Neutral fallback for unmapped vertices
        self.voltageCTF.AddRGBPoint(-1.0, 1.0, 0.95, 0.85)  # skin tone

        self.voltageCTF.SetColorSpaceToRGB()
        self.voltageCTF.SetScaleToLinear()
        self.voltageCTF.Build()

        # ------------------------------------------------------------------
        # Apply to procedural color node
        # ------------------------------------------------------------------
        self.voltageColorNode.SetAndObserveColorTransferFunction(self.voltageCTF)
        dn.SetAndObserveColorNodeID(self.voltageColorNode.GetID())
        dn.SetScalarVisibility(True)

        # ------------------------------------------------------------------
        # CRITICAL: force display node to use COLOR NODE RANGE
        # ------------------------------------------------------------------
        try:
            dn.SetScalarRangeFlag(slicer.vtkMRMLDisplayNode.UseColorNodeScalarRange)
        except Exception:
            # Fallback for older Slicer versions
            dn.SetScalarRange(low, high)

        dn.Modified()

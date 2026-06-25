# ruff: isort: skip_file
import numpy as np
import vtk
import slicer
from vtkmodules.util import numpy_support
import logging


class GeometryInterpolator:
    """
    Activation Time Mapping  -> Shepard interpolation (global, original behavior)
    Voltage Mapping          -> CARTO-style Gaussian smoothing

    This class performs all geometry-space interpolation and smoothing
    for the RA clone. It does NOT touch UI widgets.
    """

    def __init__(self, pNode, ra_clone_manager, color_mapper, sceneManager):
        self.pNode = pNode
        self.raCloneManager = ra_clone_manager
        self.colorMapper = color_mapper
        self.sceneManager = sceneManager

        self.RA = None
        self.clonedRA = None

    # ------------------------------------------------------------------
    # MAIN ENTRY
    # ------------------------------------------------------------------
    def run(self, markupsNode):
        # Replay mode: never recompute live mapping
        if getattr(self.pNode, "replayModeActive", False):
            return

        if not markupsNode:
            return

        n = markupsNode.GetNumberOfControlPoints()
        if n == 0:
            # neutral state handled elsewhere
            return

        # Optional: enforce numeric labels on-the-fly (belt-and-suspenders)
        # Local logic mirrors RAFlutterWidget._sanitize_numeric_label semantics
        for i in range(n):
            raw = markupsNode.GetNthControlPointLabel(i)
            if raw is None:
                clean = "0.0"
            else:
                s = raw.strip().replace(",", ".")
                try:
                    float(s)
                    clean = s
                except Exception:
                    clean = "0.0"
            if clean != raw:
                markupsNode.SetNthControlPointLabel(i, clean)

        # Resolve RA + clone deterministically
        self.RA = self.raCloneManager._refresh_ra()
        self.clonedRA = self.raCloneManager.ensure_clone()

        if not self.RA or not self.RA.GetPolyData():
            logging.error("GeometryInterpolator.run: RA model missing or invalid")
            return
        if not self.clonedRA:
            logging.error("GeometryInterpolator.run: RA clone missing")
            return

        ra_poly = self.RA.GetPolyData()
        if not ra_poly or ra_poly.GetNumberOfPoints() == 0:
            return

        mode = getattr(self.pNode, "mappingMode", "Activation Time Mapping")

        # ==================================================================
        # ACTIVATION TIME MAPPING - ORIGINAL SHEPARD + SASCHA'S RAINBOW
        # ==================================================================
        if mode == "Activation Time Mapping":
            if n < 2:
                # 0/1 point -> neutral/gray handled by RACloneManager
                return

            # Extract coordinates
            coords = slicer.util.arrayFromMarkupsControlPoints(markupsNode)
            if coords is None or len(coords) != n:
                logging.error(
                    f"Activation run: coordinate count mismatch: {len(coords) if coords is not None else 'None'} vs {n}"
                )
                return

            # Build LAT array
            lat_arr, scalar_name = self._build_activation_array(markupsNode, n)

            # Source polydata (mapping points)
            src = vtk.vtkPolyData()
            pts = vtk.vtkPoints()
            pts.SetData(numpy_support.numpy_to_vtk(coords))
            src.SetPoints(pts)
            src.GetPointData().AddArray(lat_arr)
            src.GetPointData().SetActiveScalars(scalar_name)

            # Shepard kernel - original behavior
            kernel = vtk.vtkShepardKernel()
            kernel.SetPowerParameter(3)
            kernel.SetKernelFootprintToNClosest()

            interp = vtk.vtkPointInterpolator()
            interp.SetKernel(kernel)
            interp.SetInputData(ra_poly)
            interp.SetSourceData(src)
            interp.SetNullPointsStrategyToClosestPoint()
            interp.Update()

            # Copy result into clone
            poly_copy = vtk.vtkPolyData()
            poly_copy.DeepCopy(interp.GetOutput())

            pd = poly_copy.GetPointData()
            arr_final = pd.GetArray(scalar_name) if pd else None
            if not arr_final:
                return

            min_v, max_v = arr_final.GetRange()
            if min_v > max_v:
                min_v, max_v = max_v, min_v

            self.clonedRA.SetAndObservePolyData(poly_copy)

            dn = self.clonedRA.GetDisplayNode()
            if dn:
                dn.SetActiveScalarName(scalar_name)
                dn.SetScalarRange(min_v, max_v)
                try:
                    dn.SetScalarRangeFlag(slicer.vtkMRMLDisplayNode.UseDataScalarRange)
                except Exception:
                    dn.SetScalarRangeFlag(0)

            # Apply Sascha's Rainbow to clone
            self.colorMapper.apply_activation_colormap(self.clonedRA)

            # Heatmap state (n >= 2)
            self.raCloneManager.set_heatmap_state()

            # Legends
            if hasattr(self.sceneManager, "updateRightAtrialColormap"):
                self.sceneManager.updateRightAtrialColormap()

            self.clonedRA.Modified()
            return

        # ==================================================================
        # VOLTAGE MAPPING - CARTO-STYLE GAUSSIAN + WINDOW
        # ==================================================================
        # Extract coordinates once
        coords = slicer.util.arrayFromMarkupsControlPoints(markupsNode)
        if coords is None or len(coords) != n:
            logging.error(
                f"Voltage run: coordinate count mismatch: {len(coords) if coords is not None else 'None'} vs {n}"
            )
            return

        radius = getattr(self.pNode, "cartoDistanceThresholdMm", 7.0)
        sharpness = getattr(self.pNode, "cartoGaussianSharpness", 3.0)

        # 1) Gaussian smoothing on RA surface
        smoothed_poly = self._smooth_voltage(ra_poly, markupsNode, radius, sharpness)

        # 2) Dual compression (low/high cutoffs)
        self._apply_voltage_dual_compression(smoothed_poly, "Voltage")

        # 3) Dense scar enforcement (optional but recommended)
        self._apply_dense_scar_enforcement(smoothed_poly, markupsNode)

        # 4) Unmapped mask (sentinel -1.0)
        self._apply_unmapped_mask(smoothed_poly, markupsNode, special_value=-1.0)

        # 5) Push into clone
        polyCopy = vtk.vtkPolyData()
        polyCopy.DeepCopy(smoothed_poly)
        self.clonedRA.SetAndObservePolyData(polyCopy)

        # 6) Voltage window from pNode
        low = getattr(self.pNode, "voltageLowCutoff", 0.1)
        high = getattr(self.pNode, "voltageHighCutoff", 0.5)

        dn = self.clonedRA.GetDisplayNode()
        if dn:
            dn.SetActiveScalarName("Voltage")
            dn.SetScalarVisibility(True)
            dn.SetOpacity(0.40)

            # Apply CARTO-style voltage colormap (normalized to [low, high])
            self.colorMapper.apply_voltage_colormap(self.clonedRA, low, high)

        # Heatmap state (n >= 2)
        self.raCloneManager.set_heatmap_state()

        # Legends
        if hasattr(self.sceneManager, "updateRightAtrialColormap"):
            self.sceneManager.updateRightAtrialColormap()

        self.clonedRA.Modified()

    # ------------------------------------------------------------------
    # Activation array builder (unchanged)
    # ------------------------------------------------------------------
    def _build_activation_array(self, markupsNode, n):
        labels = []
        for i in range(n):
            label_str = markupsNode.GetNthControlPointLabel(i)
            try:
                labels.append(float(label_str))
            except ValueError as err:
                raise ValueError(f"Non-numeric activation label at index {i}: {label_str!r}") from err

        arr = numpy_support.numpy_to_vtk(np.array(labels, dtype=float))
        arr.SetName("ActivationTime")
        return arr, "ActivationTime"

    # ------------------------------------------------------------------
    # Voltage array builder (robust parser for mappingPts labels)
    # ------------------------------------------------------------------
    def _build_voltage_array(self, markupsNode, n):
        import logging as _logging

        labels = []
        for i in range(n):
            raw = markupsNode.GetNthControlPointLabel(i)
            if raw is None:
                raw = ""
            s = raw.strip().replace(",", ".")
            try:
                v = float(s)
            except Exception:
                _logging.error(f"Voltage label at index {i} is non-numeric: {raw!r} -> using 0.0")
                v = 0.0
            labels.append(v)

        arr = numpy_support.numpy_to_vtk(np.array(labels, dtype=float))
        arr.SetName("Voltage")
        return arr, "Voltage"

    # ------------------------------------------------------------------
    # Voltage smoothing (Gaussian kernel via vtkPointInterpolator)
    # ------------------------------------------------------------------
    def _smooth_voltage(self, poly, markupsNode, radius=7.0, sharpness=3.0):
        if not markupsNode:
            return poly

        n = markupsNode.GetNumberOfControlPoints()
        if n == 0:
            return poly

        pts_np = slicer.util.arrayFromMarkupsControlPoints(markupsNode)
        if pts_np is None or len(pts_np) != n:
            logging.error(
                f"_smooth_voltage: coordinate count mismatch: {len(pts_np) if pts_np is not None else 'None'} vs {n}"
            )
            return poly

        volt_arr, name = self._build_voltage_array(markupsNode, n)

        src = vtk.vtkPolyData()
        pts = vtk.vtkPoints()
        pts.SetData(numpy_support.numpy_to_vtk(pts_np))
        src.SetPoints(pts)
        src.GetPointData().AddArray(volt_arr)
        src.GetPointData().SetActiveScalars(name)

        kernel = vtk.vtkGaussianKernel()
        kernel.SetRadius(radius)
        kernel.SetSharpness(sharpness)
        kernel.SetKernelFootprintToRadius()

        interp = vtk.vtkPointInterpolator()
        interp.SetInputData(poly)
        interp.SetSourceData(src)
        interp.SetKernel(kernel)
        interp.SetNullPointsStrategyToNullValue()
        interp.SetNullValue(0.0)
        interp.Update()

        out_poly = interp.GetOutput()
        out_pd = out_poly.GetPointData()
        out_scalars = out_pd.GetScalars() if out_pd else None

        if out_scalars:
            out_scalars.SetName("Voltage")
            out_pd.SetActiveScalars("Voltage")

        final_poly = vtk.vtkPolyData()
        final_poly.DeepCopy(out_poly)
        final_poly.GetPointData().SetActiveScalars("Voltage")
        return final_poly

    # ------------------------------------------------------------------
    # Dual compression, unmapped mask, dense scar
    # ------------------------------------------------------------------
    def _apply_voltage_dual_compression(self, poly, scalar_name="Voltage"):
        pd = poly.GetPointData()
        if not pd:
            return

        arr = pd.GetArray(scalar_name)
        if not arr:
            return

        low = getattr(self.pNode, "voltageLowCutoff", None)
        high = getattr(self.pNode, "voltageHighCutoff", None)
        if high is None:
            return
        if low is None:
            low = 0.1
        if low > high:
            low, high = high, low

        import numpy as _np
        from vtkmodules.util import numpy_support as _ns

        np_arr = _ns.vtk_to_numpy(arr).astype(float, copy=False)
        _np.clip(np_arr, low, high, out=np_arr)
        arr.Modified()

    def _apply_unmapped_mask(self, poly, markupsNode, special_value=-1.0):
        pd = poly.GetPointData()
        if not pd:
            return

        scalars = pd.GetScalars()
        if not scalars:
            return

        import numpy as _np
        from vtkmodules.util import numpy_support as _ns

        pts = poly.GetPoints()
        ra_xyz = _ns.vtk_to_numpy(pts.GetData()).astype(float)

        mp = slicer.util.arrayFromMarkupsControlPoints(markupsNode).astype(float)
        if len(mp) == 0:
            unmapped_mask = _np.ones(len(ra_xyz), dtype=bool)
        else:
            diff = ra_xyz[:, None, :] - mp[None, :, :]
            dist2 = _np.sum(diff * diff, axis=2)
            dist = _np.sqrt(dist2)

            threshold = getattr(self.pNode, "cartoDistanceThresholdMm", 7.0)
            unmapped_mask = _np.min(dist, axis=1) > threshold

        np_arr = _ns.vtk_to_numpy(scalars).astype(float, copy=False)
        np_arr[unmapped_mask] = special_value
        scalars.Modified()

    def _apply_dense_scar_enforcement(
        self, poly, markupsNode, scar_threshold=0.15, enforce_value=0.05, enforce_radius=6.0
    ):
        pd = poly.GetPointData()
        if not pd:
            return

        scalars = pd.GetScalars()
        if not scalars:
            return

        import numpy as _np
        from vtkmodules.util import numpy_support as _ns

        # RA surface points
        pts = poly.GetPoints()
        ra_xyz = _ns.vtk_to_numpy(pts.GetData()).astype(float)

        # Mapping points (coordinates)
        mp = slicer.util.arrayFromMarkupsControlPoints(markupsNode).astype(float)
        n_mp = markupsNode.GetNumberOfControlPoints()
        if len(mp) != n_mp:
            logging.error(f"_apply_dense_scar_enforcement: mp mismatch: coords={len(mp)} labels={n_mp}")
            return

        # Mapping point voltages (robust parser, no ValueError)
        volt_arr, _ = self._build_voltage_array(markupsNode, n_mp)
        mp_volt = _ns.vtk_to_numpy(volt_arr).astype(float)

        # Indices of dense scar points (below threshold)
        dense_idx = _np.where(mp_volt < scar_threshold)[0]
        if len(dense_idx) == 0:
            return

        dense_pts = mp[dense_idx]

        # Distance from each RA point to nearest dense-scar mapping point
        diff = ra_xyz[:, None, :] - dense_pts[None, :, :]
        dist2 = _np.sum(diff * diff, axis=2)
        dist = _np.sqrt(dist2)

        enforce_mask = _np.min(dist, axis=1) < enforce_radius

        # Enforce low voltage in dense-scar neighborhood
        np_arr = _ns.vtk_to_numpy(scalars).astype(float, copy=False)
        np_arr[enforce_mask] = enforce_value
        scalars.Modified()

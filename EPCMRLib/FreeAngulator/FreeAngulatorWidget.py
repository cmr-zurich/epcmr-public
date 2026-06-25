import qt
import slicer
from slicer.ScriptedLoadableModule import ScriptedLoadableModuleWidget
from .FreeAngulatorLogic import FreeAngulatorLogic
from EPCMRLib.EPCMRParameterNode import EPCMRParameterNode


# ----------------------------------------------------------------------
# GEOMETRY HELPERS (robust across PythonQt variants)
# ----------------------------------------------------------------------


def _rect_components(rect):
    """Extract x, y, width, height from a QRect-like object."""
    x_attr = getattr(rect, "x", None)
    x = x_attr() if callable(x_attr) else int(x_attr) if x_attr is not None else 0

    y_attr = getattr(rect, "y", None)
    y = y_attr() if callable(y_attr) else int(y_attr) if y_attr is not None else 0

    w_attr = getattr(rect, "width", None)
    w = w_attr() if callable(w_attr) else int(w_attr) if w_attr is not None else 0

    h_attr = getattr(rect, "height", None)
    h = h_attr() if callable(h_attr) else int(h_attr) if h_attr is not None else 0

    return x, y, w, h


def _widget_pos(widget):
    """Return (x, y) robustly across PythonQt variants."""
    p_attr = getattr(widget, "pos", None)
    if callable(p_attr):
        p = p_attr()
        return p.x(), p.y()
    if isinstance(p_attr, qt.QPoint):
        return p_attr.x(), p_attr.y()
    return 0, 0


# ----------------------------------------------------------------------
# MAIN WIDGET
# ----------------------------------------------------------------------


class FreeAngulatorWidget(qt.QWidget):
    """
    Floating UI widget for Free Angulator.

    Behavior:
      - FIRST appearance in a session: 40 px from left
      - After user moves it: remember position for the rest of the session
      - Across sessions: restore from QSettings
      - No forced repositioning in showEvent

    IMPORTANT:
      This version is deliberately **non-invasive** with respect to Slicer's
      internal slice pipeline and Thick Slab reconstruction:

        - NO observers on slice/composite/display nodes
        - NO renderRequested hooks
        - NO slab-mode checks or overrides

      It only:
        - enables slice intersections at startup
        - sets medium line thickness at startup
        - sets medium line thickness for the active slice when free angulation
          is enabled

      Thick Slab reconstruction is therefore left entirely to Slicer.
    """

    # ------------------------------------------------------------------
    # INIT
    # ------------------------------------------------------------------
    def __init__(self, parent=None):
        if parent is None:
            parent = slicer.util.mainWindow()
        super().__init__(parent)

        self.setWindowTitle("Free Angulator")
        # Disable maximize button (keep close + optional minimize)
        self.setWindowFlags(qt.Qt.Window | qt.Qt.WindowStaysOnTopHint | qt.Qt.CustomizeWindowHint | qt.Qt.WindowCloseButtonHint)

        self.setMinimumWidth(420)

        self.logic = FreeAngulatorLogic()
        self.buildUI()

        # ---------------------------------------------------------
        # Lock window size ONCE after UI is built
        # NOTE:
        #   - sizeHint is a QSize property in this Slicer/PythonQt build
        #   - We widen the window BEFORE locking the size
        # ---------------------------------------------------------
        self.adjustSize()
        hint = self.sizeHint  # QSize property

        # Double the width (or adjust as needed; we use 1.6)
        w = int(hint.width() * 1.6)
        h = hint.height()

        # Apply widened fixed size
        self.setFixedSize(w, h)

        # ---------------------------------------------------------
        # Restore last window position OR place on the LEFT side
        # ---------------------------------------------------------
        settings = qt.QSettings()
        pos = settings.value("FreeAngulator/pos")

        if isinstance(pos, qt.QPoint):
            self.move(pos)
        elif isinstance(pos, (list, tuple)) and len(pos) == 2:
            self.move(int(pos[0]), int(pos[1]))
        else:
            # FIRST TIME EVER → place at 40 px left
            self._applyLeftSidePlacement()

    # ------------------------------------------------------------------
    # showEvent REMOVED
    # ------------------------------------------------------------------
    # PythonQt does NOT expose QWidget.showEvent(), and attempts to
    # override it caused crashes and freezes. Window size is now locked
    # once in __init__() via setFixedSize(sizeHint), which is stable.

    # ------------------------------------------------------------------
    # LEFT-SIDE INITIAL PLACEMENT
    # ------------------------------------------------------------------
    def _applyLeftSidePlacement(self):
        """Place widget 40 px from left edge on FIRST appearance only."""
        mw = slicer.util.mainWindow()
        if not mw:
            return

        fg = getattr(mw, "frameGeometry", None)
        if isinstance(fg, qt.QRect):
            geo = fg
        elif callable(fg):
            geo = fg()
        else:
            geo = None

        if isinstance(geo, qt.QRect):
            gx, gy, gw, gh = _rect_components(geo)
            targetX = gx + 40
            targetY = gy + 120
            self.move(targetX, targetY)

    # ------------------------------------------------------------------
    # SAVE WINDOW POSITION
    # ------------------------------------------------------------------
    def moveEvent(self, event):
        """
        Save window position whenever the user moves the Free Angulator window.

        NOTE:
        PythonQt's QWidget base does NOT expose moveEvent to super(),
        so we do NOT call super().moveEvent(event).
        """
        settings = qt.QSettings()
        x, y = _widget_pos(self)
        settings.setValue("FreeAngulator/pos", qt.QPoint(x, y))

    # ------------------------------------------------------------------
    # EPCMRParameterNode integration
    # ------------------------------------------------------------------
    def getEPCMRParameterNodeWrapper(self):
        try:
            node = slicer.mrmlScene.GetFirstNodeByName("EPCMRParameterNode")
            if node:
                return EPCMRParameterNode(node)
        except Exception:
            pass
        return None

    # ------------------------------------------------------------------
    # Startup slice intersection environment
    # ------------------------------------------------------------------
    def initializeSliceIntersectionEnvironment(self):
        """
        Ensure that slice intersections are visible, interactive, and use
        medium-thickness lines. Also ensure that all slices start in a common
        ViewGroup so intersections are drawn immediately when FreeAngulator
        is opened.

        IMPORTANT:
        This function **does not** touch slab mode or any slab-related
        properties, so Thick Slab reconstruction remains fully functional.
        """

        # --------------------------------------------------------------
        # 1. Enable slice intersections (CompositeNodes)
        # --------------------------------------------------------------
        for compNode in slicer.util.getNodesByClass("vtkMRMLSliceCompositeNode"):
            try:
                compNode.SetSliceIntersectionVisibility(1)
                compNode.Modified()
            except AttributeError:
                pass

        # --------------------------------------------------------------
        # 2. Enable interaction + medium lines (SliceDisplayNodes)
        # --------------------------------------------------------------
        for displayNode in slicer.util.getNodesByClass("vtkMRMLSliceDisplayNode"):
            try:
                displayNode.SetIntersectingSlicesInteractive(True)
            except AttributeError:
                pass

            try:
                # 0 = fine, 1 = medium, 2 = thick
                displayNode.SetIntersectingSlicesLineThicknessMode(1)
            except AttributeError:
                pass

        # --------------------------------------------------------------
        # 3. Normalize ViewGroups so intersections are visible initially
        # --------------------------------------------------------------
        for sliceNode in slicer.util.getNodesByClass("vtkMRMLSliceNode"):
            try:
                sliceNode.SetViewGroup(0)
            except AttributeError:
                pass

        # --------------------------------------------------------------
        # 4. Simple redraw (no pipeline override)
        # --------------------------------------------------------------
        slicer.util.forceRenderAllViews()

    # ------------------------------------------------------------------
    # UI construction
    # ------------------------------------------------------------------
    def buildUI(self):
        # Initialize intersection environment before any FreeAngulator logic
        self.initializeSliceIntersectionEnvironment()

        self.ensureInteractionModeViewTransform()
        self.ensureInteractionToolbarButtonChecked()
        self.ensureSliceIntersectionsVisible()

        layout = qt.QVBoxLayout(self)

        label = qt.QLabel("<b>Free Slice Angulation</b>")
        layout.addWidget(label)

        # ---------------------------------------------------------
        # Free buttons
        # ---------------------------------------------------------
        buttonLayout1 = qt.QHBoxLayout()

        btnRed = qt.QPushButton("Free Red")
        btnGreen = qt.QPushButton("Free Green")
        btnYellow = qt.QPushButton("Free Yellow")

        for btn in (btnRed, btnGreen, btnYellow):
            btn.setCheckable(True)

        buttonGroup = qt.QButtonGroup(self)
        buttonGroup.setExclusive(True)
        buttonGroup.addButton(btnRed)
        buttonGroup.addButton(btnGreen)
        buttonGroup.addButton(btnYellow)

        btnRed.setStyleSheet("QPushButton { color: #ff4444; } QPushButton:checked { font-weight: bold; }")
        btnGreen.setStyleSheet("QPushButton { color: #44ff44; } QPushButton:checked { font-weight: bold; }")
        btnYellow.setStyleSheet("QPushButton { color: #ffff66; } QPushButton:checked { font-weight: bold; }")

        buttonLayout1.addWidget(btnRed)
        buttonLayout1.addWidget(btnGreen)
        buttonLayout1.addWidget(btnYellow)
        layout.addLayout(buttonLayout1)

        btnRed.clicked.connect(lambda checked=False: self.setFreeAngulation("Red"))
        btnGreen.clicked.connect(lambda checked=False: self.setFreeAngulation("Green"))
        btnYellow.clicked.connect(lambda checked=False: self.setFreeAngulation("Yellow"))

        # ---------------------------------------------------------
        # Restore ViewGroups (NO orientation reset)
        # ---------------------------------------------------------
        btnRestore = qt.QPushButton("Restore ViewGroups")
        btnRestore.setStyleSheet("QPushButton { color: #cccccc; } QPushButton:hover { color: white; }")
        layout.addWidget(btnRestore)

        btnRestore.clicked.connect(
            lambda checked=False, bR=btnRed, bG=btnGreen, bY=btnYellow, bg=buttonGroup: self.restoreViewGroupsAndUI(
                bR, bG, bY, bg
            )
        )

        # ---------------------------------------------------------
        # Restore Orthogonal Views
        # ---------------------------------------------------------
        btnOrtho = qt.QPushButton("Restore Orthogonal Views")
        btnOrtho.setStyleSheet("QPushButton { color: #cccccc; } QPushButton:hover { color: white; }")
        layout.addWidget(btnOrtho)

        btnOrtho.clicked.connect(self.restoreOrthogonalViews)

        # ---------------------------------------------------------
        # Geometry management
        # ---------------------------------------------------------
        btnStoreGeom = qt.QPushButton("Store Target Geometry")
        btnStoreGeom.setStyleSheet("QPushButton { color: #88ccff; } QPushButton:hover { color: white; }")
        layout.addWidget(btnStoreGeom)

        btnRestoreGeom = qt.QPushButton("Restore Geometry")
        btnRestoreGeom.setStyleSheet("QPushButton { color: #88ccff; } QPushButton:hover { color: white; }")
        layout.addWidget(btnRestoreGeom)

        btnDeleteGeom = qt.QPushButton("Delete Geometry")
        btnDeleteGeom.setStyleSheet("QPushButton { color: #ff6666; } QPushButton:hover { color: white; }")
        layout.addWidget(btnDeleteGeom)

        comboGeom = qt.QComboBox()
        comboGeom.setMinimumWidth(200)
        layout.addWidget(comboGeom)

        # Populate dropdown
        def refreshGeometryList():
            comboGeom.clear()
            for n in self.logic.listGeometries():
                comboGeom.addItem(n)

        refreshGeometryList()

        # Store geometry
        def onStoreGeometry():
            dialog = qt.QInputDialog(self)
            dialog.setWindowTitle("Store Geometry")
            dialog.setLabelText("Enter target geometry name:")
            dialog.setTextValue("")
            dialog.setInputMode(qt.QInputDialog.TextInput)
            dialog.setTextEchoMode(qt.QLineEdit.Normal)
            ok = dialog.exec_()

            if not ok:
                return

            name = str(dialog.textValue()).strip()
            if not name:
                return

            self.logic.storeTargetGeometry(name)
            refreshGeometryList()

        btnStoreGeom.clicked.connect(onStoreGeometry)

        # Restore geometry
        btnRestoreGeom.clicked.connect(lambda checked=False: self.logic.restoreTargetGeometry(comboGeom.currentText))

        # Delete geometry
        def onDeleteGeometry():
            name = comboGeom.currentText
            if not name:
                return

            reply = qt.QMessageBox.question(
                self, "Delete Geometry", f"Delete stored geometry '{name}'?", qt.QMessageBox.Yes | qt.QMessageBox.No
            )

            if reply == qt.QMessageBox.Yes:
                self.logic.deleteTargetGeometry(name)
                refreshGeometryList()

        btnDeleteGeom.clicked.connect(onDeleteGeometry)

        print("Free Angulator floating panel opened.")

        # NOTE:
        # We intentionally do NOT install slice observers or renderRequested
        # hooks here, to avoid interfering with Thick Slab reconstruction.
        # FreeAngulator now relies on:
        #   - startup medium thickness
        #   - per-slice medium thickness when activating free angulation
        # and leaves Slicer's own interaction/slab logic untouched.

    # ----------------------------------------------------------------------
    # Utility methods
    # ----------------------------------------------------------------------
    def ensureInteractionModeViewTransform(self):
        slicer.app.applicationLogic().GetInteractionNode().SetCurrentInteractionMode(
            slicer.vtkMRMLInteractionNode.ViewTransform
        )

    def ensureInteractionToolbarButtonChecked(self):
        mw = slicer.util.mainWindow()
        if not mw:
            return
        action = mw.findChild(qt.QAction, "View/AdjustViewAction")
        if action:
            action.blockSignals(True)
            action.setChecked(True)
            action.blockSignals(False)

    def ensureSliceIntersectionsVisible(self):
        lm = slicer.app.layoutManager()
        if not lm:
            return

        for sliceViewName in ("Red", "Green", "Yellow"):
            sw = lm.sliceWidget(sliceViewName)
            if not sw:
                continue

            logic = sw.sliceLogic()
            if not logic:
                continue

            try:
                logic.SetSliceIntersectionsVisible(True)
            except AttributeError:
                try:
                    logic.SliceIntersectionsVisibleOn()
                except AttributeError:
                    pass

    # ----------------------------------------------------------------------
    # Free slice angulation
    # ----------------------------------------------------------------------
    def setFreeAngulation(self, sliceName):
        """
        Activate free angulation for the selected slice.

        This function:
          - assigns ViewGroup 2 to the active slice
          - assigns ViewGroup 1 to the others
          - enables linked control
          - sets medium line thickness for the active slice

        It does **not** touch slab mode, so Thick Slab reconstruction
        remains fully functional.
        """
        lm = slicer.app.layoutManager()
        if not lm:
            return

        epcmrNode = self.getEPCMRParameterNodeWrapper()
        if epcmrNode:
            epcmrNode.freeAngulatorActiveSlice = sliceName

        for name in ("Red", "Green", "Yellow"):
            sw = lm.sliceWidget(name)
            if not sw:
                continue

            logic = sw.sliceLogic()
            sliceNode = logic.GetSliceNode()
            compNode = logic.GetSliceCompositeNode()

            if name == sliceName:
                # Active slice goes to ViewGroup 2
                sliceNode.SetViewGroup(2)
                compNode.SetLinkedControl(True)

                # ---------------------------------------------------------
                # THICKNESS MODE API (medium lines, Slicer 5.7 compatible)
                # NOTE:
                #   Your Slicer build does NOT support:
                #       sliceNode.GetSliceDisplayNode()
                #   Therefore we MUST use the ID-based lookup.
                # ---------------------------------------------------------
                sliceDisplayNode = None
                try:
                    dnID = sliceNode.GetSliceDisplayNodeID()
                    if dnID:
                        sliceDisplayNode = slicer.mrmlScene.GetNodeByID(dnID)
                except AttributeError:
                    sliceDisplayNode = None

                if sliceDisplayNode:
                    try:
                        # 0 = fine, 1 = medium, 2 = thick
                        sliceDisplayNode.SetIntersectingSlicesLineThicknessMode(1)
                    except AttributeError:
                        pass

            else:
                # Non-active slices go to ViewGroup 1
                sliceNode.SetViewGroup(1)
                compNode.SetLinkedControl(True)

        slicer.util.forceRenderAllViews()

    # ----------------------------------------------------------------------
    # Orthogonal restore
    # ----------------------------------------------------------------------
    def restoreOrthogonalViews(self):
        lm = slicer.app.layoutManager()
        for sliceName, orientation in (
            ("Red", "Axial"),
            ("Green", "Coronal"),
            ("Yellow", "Sagittal"),
        ):
            sw = lm.sliceWidget(sliceName)
            if not sw:
                continue
            sw.sliceLogic().GetSliceNode().SetOrientation(orientation)
        slicer.util.forceRenderAllViews()

    # ----------------------------------------------------------------------
    # Restore ViewGroups (NO orientation reset)
    # ----------------------------------------------------------------------
    def restoreViewGroupsAndUI(self, btnRed, btnGreen, btnYellow, buttonGroup):
        lm = slicer.app.layoutManager()
        epcmrNode = self.getEPCMRParameterNodeWrapper()

        if lm:
            for name in ("Red", "Green", "Yellow"):
                sw = lm.sliceWidget(name)
                if not sw:
                    continue
                sliceNode = sw.sliceLogic().GetSliceNode()
                sliceNode.SetViewGroup(1)

            # ---------------------------------------------------------
            # Disable slice linking so shortcut "v" only affects the clicked slice
            # ---------------------------------------------------------
            for name in ("Red", "Green", "Yellow"):
                sw = lm.sliceWidget(name)
                if not sw:
                    continue
                compNode = sw.sliceLogic().GetSliceCompositeNode()
                compNode.SetLinkedControl(False)

        btnRed.blockSignals(True)
        btnGreen.blockSignals(True)
        btnYellow.blockSignals(True)

        btnRed.setChecked(False)
        btnGreen.setChecked(False)
        btnYellow.setChecked(False)

        btnRed.blockSignals(False)
        btnGreen.blockSignals(False)
        btnYellow.blockSignals(False)

        buttonGroup.setExclusive(False)
        btnRed.setChecked(False)
        btnGreen.setChecked(False)
        btnYellow.setChecked(False)
        buttonGroup.setExclusive(True)

        if epcmrNode:
            epcmrNode.freeAngulatorActiveSlice = ""

        slicer.util.forceRenderAllViews()

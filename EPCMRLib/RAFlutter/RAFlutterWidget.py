# ruff: isort: skip_file

# ---- Core "V-Q-C-S" Stack ----
import vtk
import qt
import ctk
import slicer
import logging
import os

# ---- Local Libs ----
from .RAFlutterLogic import RAFlutterLogic
from EPCMRLib.EPCMRParameterNode import EPCMRParameterNode
from EPCMRLib.Mapping.MappingModeSelector import MappingModeSelector


class RAFlutterWidget(qt.QWidget):
    """
    UI + interaction layer for RA Flutter mapping.
    Uses the NEW architecture:
      - RACloneManager (clone + visibility)
      - GeometryInterpolator (AT + Voltage)
      - ColorMapper (LUTs)
      - MappingEventController (MRML observers)
    """

    def __init__(self, logic, pNode: EPCMRParameterNode, mainWidget=None, parent=None, getReplayer=None):
        super().__init__(parent)

        # --- 1. Shared EPCMRLogic instance ---
        self.logic = logic
        self.mainLogic = logic

        # --- 2. Wrapped EPCMRParameterNode ---
        self.pn = pNode
        if type(self.pn).__name__ != "EPCMRParameterNode":
            raise TypeError(f"RAFlutterWidget expected wrapped EPCMRParameterNode, got {type(self.pn)}")

        # --- 3. Validate logic instance ---
        if not hasattr(logic, "_parameterNodeWrapped"):
            raise RuntimeError("RAFlutterWidget received invalid logic instance (missing wrapped parameter node)")

        # --- 4. Backlink to main widget ---
        self.mainWidget = mainWidget

        # --- 5. Lazy replayer factory (NEW) ---
        #
        # IMPORTANT:
        # ----------
        # RAFlutterWidget no longer creates CatheterReplayer directly.
        # Instead, it receives a callable (logic.getReplayer) that will
        # create or return the replayer *lazily* when needed.
        #
        # This guarantees:
        #   - SceneManager has already loaded models
        #   - transforms exist
        #   - LUTs exist
        #   - no early initialization
        #
        self.getReplayer = getReplayer

        # --- 6. Internal state ---
        self.replayer = None
        self.deletedPointsStack = []
        self._lastSelectedPointInfo = None
        self._isProgrammaticDelete = False

        # --- 7. Build UI ---
        self.setup()

    # ------------------------------------------------------------------
    # Internal UI helper class: thin horizontal divider (safe, no styled-mode)
    # ------------------------------------------------------------------
    class _ThinDivider(qt.QWidget):
        def __init__(self, parent=None):
            super().__init__(parent)
            self.setFixedHeight(1)

        def paintEvent(self, event):
            # Guard: avoid painting when widget has no width (Slicer can call early)
            w = self.width
            if w <= 1:
                return

            painter = qt.QPainter(self)
            if not painter.isActive():
                return  # safety: avoid rare null-surface crash

            painter.setRenderHint(qt.QPainter.Antialiasing, False)

            pen = qt.QPen(qt.QColor(180, 180, 180, 90))
            pen.setWidth(1)
            painter.setPen(pen)

            # Draw a single horizontal pixel line
            painter.drawLine(0, 0, w, 0)

    @staticmethod
    def _sanitize_numeric_label(raw_label, fallback_value="0.0"):
        """
        Ensure mappingPts labels are always numeric.
        Returns a float converted to string; never raises, never logs.
        """
        if raw_label is None:
            return fallback_value

        s = raw_label.strip().replace(",", ".")
        try:
            float(s)
            return s
        except Exception:
            return fallback_value

    # ----------------------------------------------------------------------
    # POINT OBSERVERS
    # ----------------------------------------------------------------------
    def attachPointObservers(self):
        """
        Ensures RAFlutterWidget always observes the *current* mappingPts and ablationPts
        nodes in the scene -- even after MappingEventController replaces them.

        Additionally:
          - As soon as the SECOND mapping point is placed (mappingPts has >= 2 points),
            SceneManager.updateRightAtrialColormap() is called so that the color legend
            scalar bar appears without requiring a subsequent move.
        """
        if type(self.pn).__name__ != "EPCMRParameterNode":
            raise RuntimeError("attachPointObservers() received RAW MRML node instead of wrapper")

        if not hasattr(self, "_observerTags"):
            self._observerTags = {}

        for nodeName in ["mappingPts", "ablationPts"]:
            node = slicer.util.getFirstNodeByName(nodeName) or getattr(self.pn, nodeName, None)
            if not node:
                continue

            # Remove old observers
            if nodeName in self._observerTags:
                for tag in self._observerTags[nodeName]:
                    try:
                        node.RemoveObserver(tag)
                    except:  # noqa: E722
                        pass

            # Add fresh observers
            startTag = node.AddObserver(
                slicer.vtkMRMLMarkupsNode.PointStartInteractionEvent,
                self._onPointStartInteraction,
            )
            removedTag = node.AddObserver(
                slicer.vtkMRMLMarkupsNode.PointRemovedEvent,
                self._onPointRemoved,
            )
            endTag = node.AddObserver(
                slicer.vtkMRMLMarkupsNode.PointEndInteractionEvent,
                self._onPointEndInteraction,
            )

            # NEW: react immediately when a point is ADDED
            # For mappingPts only: once we have at least 2 points, trigger RA colormap update
            def onPointAdded(caller, event, name=nodeName):
                if name != "mappingPts":
                    return
                try:
                    if caller.GetNumberOfControlPoints() >= 2:
                        if hasattr(self.logic, "sceneManager") and self.logic.sceneManager:
                            self.logic.sceneManager.updateRightAtrialColormap()
                except Exception:
                    # Hard-fail-safe: never break markups interaction due to legend update
                    pass

            addedTag = node.AddObserver(
                slicer.vtkMRMLMarkupsNode.PointAddedEvent,
                onPointAdded,
            )

            self._observerTags[nodeName] = [startTag, removedTag, endTag, addedTag]

    def applyOpticalLeftAlignment(self, label: qt.QLabel):
        """
        Automatically correct optical left-edge misalignment caused by
        glyph side-bearing differences (e.g., 'M' vs 'V') using
        QFontMetrics.horizontalAdvance() instead of hard-coded offsets.

        This produces true optical alignment across all fonts and DPI.
        """
        text = label.text.lstrip()  # .text is a property in Slicer's Qt
        if not text:
            return

        first = text[0]

        # Obtain font metrics for the label's current font
        fm = qt.QFontMetrics(label.font)

        # Measure visual width of the first glyph vs reference glyph 'M'
        first_width = fm.horizontalAdvance(first)
        ref_width = fm.horizontalAdvance("M")

        # Compute optical offset: how much narrower the first glyph is
        # Negative margin shifts the glyph left to match 'M'
        margin = first_width - ref_width

        # Apply margin-left correction
        current = label.styleSheet
        label.setStyleSheet(current + f" margin-left: {margin}px;")

    # ----------------------------------------------------------------------
    # UI SETUP
    # ----------------------------------------------------------------------
    def setup(self):
        self.setLayout(qt.QVBoxLayout())
        layout = self.layout()
        layout.setSpacing(6)
        layout.setContentsMargins(0, 0, 0, 0)

        # ============================================================
        # Catheter Replay
        # ============================================================
        self.replayerCollapsibleButton = ctk.ctkCollapsibleButton()
        self.replayerCollapsibleButton.text = "Catheter Replay"
        layout.addWidget(self.replayerCollapsibleButton)

        replayerContainer = qt.QWidget()
        replayerFormLayout = qt.QFormLayout(replayerContainer)
        self.replayerCollapsibleButton.setLayout(qt.QVBoxLayout())
        self.replayerCollapsibleButton.layout().addWidget(replayerContainer)

        self.launchReplayerButton = qt.QPushButton("Show Replayer Controls")
        self.launchReplayerButton.setCheckable(True)
        replayerFormLayout.addRow(self.launchReplayerButton)
        self.launchReplayerButton.clicked.connect(self.onLaunchReplayer)

        # ============================================================
        # Mapping Management
        # ============================================================
        self.mappingCollapsibleButton = ctk.ctkCollapsibleButton()
        self.mappingCollapsibleButton.text = "Mapping Management"
        self.mappingCollapsibleButton.collapsed = False
        layout.addWidget(self.mappingCollapsibleButton)

        mappingContainer = qt.QWidget()

        # NOTE: --- THIS IS CRITICAL (!) ---
        # VERTICAL GUIDE FOR DEBUGGING
        # Applying a stylesheet here forces Qt into styled-mode and flattens all CTK sliders.
        # mappingContainer.setStyleSheet("background: transparent; border-left: 2px solid rgba(255,0,0,0.35);")

        mappingLayout = qt.QGridLayout(mappingContainer)
        mappingLayout.setContentsMargins(8, 4, 8, 8)
        mappingLayout.setHorizontalSpacing(10)
        mappingLayout.setVerticalSpacing(6)
        mappingLayout.setColumnStretch(0, 0)
        mappingLayout.setColumnStretch(1, 1)

        self.mappingCollapsibleButton.setLayout(qt.QVBoxLayout())
        self.mappingCollapsibleButton.layout().addWidget(mappingContainer)

        row = 0

        # ----------------------------------------------------------------------
        # Mapping Mode Selector
        # ----------------------------------------------------------------------
        self.mappingModeSelector = MappingModeSelector(self.pn)

        modeLabel = qt.QLabel("Mapping Mode:")
        modeLabel.setStyleSheet("font-weight: bold; color: #CCCCCC;")
        self.applyOpticalLeftAlignment(modeLabel)

        mappingLayout.addWidget(modeLabel, row, 0)
        mappingLayout.addWidget(self.mappingModeSelector, row, 1)

        self.mappingModeSelector.mappingModeChanged.connect(self.onMappingModeChanged)

        currentMode = getattr(self.pn, "mappingMode", "Activation Time Mapping")
        if currentMode not in ["Activation Time Mapping", "Voltage Mapping"]:
            currentMode = "Activation Time Mapping"
            self.pn.mappingMode = currentMode

        row += 1

        # ----------------------------------------------------------------------
        # Mapping Phase
        # ----------------------------------------------------------------------
        mappingPhaseLabel = qt.QLabel("Mapping Phase:")
        mappingPhaseLabel.setStyleSheet("font-weight: bold; color: #CCCCCC;")
        self.applyOpticalLeftAlignment(mappingPhaseLabel)

        self.mappingPhaseGroup = qt.QButtonGroup()
        self.prePhaseRadio = qt.QRadioButton("PRE-ablation")
        self.postPhaseRadio = qt.QRadioButton("POST-ablation")
        self.mappingPhaseGroup.addButton(self.prePhaseRadio)
        self.mappingPhaseGroup.addButton(self.postPhaseRadio)
        self.postPhaseRadio.setChecked(True)

        phaseStyle = """
                    QRadioButton {
                        padding: 2px 6px;
                        border-radius: 4px;
                        border: 1px solid #555555;
                    }
                    QRadioButton:checked {
                        background-color: #3A4A5A;
                        color: white;
                        font-weight: bold;
                    }
                    QRadioButton:!checked {
                        background-color: transparent;
                        color: #dddddd;
                    }
                """

        toggleStyle = """
                QPushButton {
                    padding: 2px 6px;
                    border-radius: 4px;
                    border: 1px solid #555555;
                    background-color: transparent;
                    color: #dddddd;
                }
                QPushButton:checked {
                    background-color: #3A4A5A;
                    color: white;
                }
                QPushButton:hover {
                    border: 1px solid #777777;
                }
                """

        self.prePhaseRadio.setStyleSheet(phaseStyle)
        self.postPhaseRadio.setStyleSheet(phaseStyle)

        phaseFieldWidget = qt.QWidget()
        phaseFieldLayout = qt.QHBoxLayout(phaseFieldWidget)
        phaseFieldLayout.setContentsMargins(0, 0, 0, 0)
        phaseFieldLayout.setSpacing(6)
        phaseFieldLayout.addWidget(self.prePhaseRadio)
        phaseFieldLayout.addWidget(self.postPhaseRadio)
        phaseFieldLayout.addStretch(1)

        mappingLayout.addWidget(mappingPhaseLabel, row, 0)
        mappingLayout.addWidget(phaseFieldWidget, row, 1)

        self.prePhaseRadio.toggled.connect(self.onMappingPhaseChanged)

        row += 1

        # ----------------------------------------------------------------------
        # Mapping Parameters - Activation (header + inline explanation)
        # ----------------------------------------------------------------------
        activationHeaderWidget = qt.QWidget()
        activationHeaderLayout = qt.QHBoxLayout(activationHeaderWidget)
        activationHeaderLayout.setContentsMargins(0, 0, 0, 0)
        activationHeaderLayout.setSpacing(6)

        self.activationHeader = qt.QLabel("Mapping Parameters - Activation:")
        self.activationHeader.setStyleSheet("font-weight: bold; color: #CCCCCC; margin-top: 8px;")
        self.applyOpticalLeftAlignment(self.activationHeader)

        self.activationInfo = qt.QLabel("No adjustable parameters for LAT mapping.")
        self.activationInfo.setStyleSheet("color: #888888; font-style: italic; margin-top: 8px;")

        activationHeaderLayout.addWidget(self.activationHeader)
        activationHeaderLayout.addWidget(self.activationInfo)
        activationHeaderLayout.addStretch(1)

        mappingLayout.addWidget(activationHeaderWidget, row, 0, 1, 2)
        row += 1

        # ----------------------------------------------------------------------
        # Thin divider between Activation and Voltage
        # ----------------------------------------------------------------------
        self.activationDivider = RAFlutterWidget._ThinDivider(self)
        mappingLayout.addWidget(self.activationDivider, row, 0, 1, 2)
        row += 1

        # ----------------------------------------------------------------------
        # Mapping Parameters - Voltage (GROUP HEADER)
        # ----------------------------------------------------------------------
        self.voltageHeader = qt.QLabel("Mapping Parameters - Voltage:")
        self.voltageHeader.setStyleSheet("font-weight: bold; color: #CCCCCC; margin-top: 8px;")
        self.applyOpticalLeftAlignment(self.voltageHeader)

        mappingLayout.addWidget(self.voltageHeader, row, 0)
        mappingLayout.addWidget(qt.QWidget(), row, 1)
        row += 1

        # ----------------------------------------------------------------------
        # Voltage High Cutoff
        # ----------------------------------------------------------------------
        self.highCutLabel = qt.QLabel("High Cutoff [mV]:")
        self.highCutLabel.setStyleSheet("font-weight: normal; color: #CCCCCC; margin-left: 12px;")

        self.highCutSlider = ctk.ctkSliderWidget()
        self.highCutSlider.singleStep = 0.01
        self.highCutSlider.minimum = 0.0
        self.highCutSlider.maximum = 5.0
        self.highCutSlider.decimals = 3
        self.highCutSlider.value = getattr(self.pn, "voltageHighCutoff", 0.5)

        highCutFieldWidget = qt.QWidget()
        highCutFieldLayout = qt.QHBoxLayout(highCutFieldWidget)
        highCutFieldLayout.setContentsMargins(0, 0, 0, 0)
        highCutFieldLayout.setSpacing(0)
        highCutFieldLayout.addWidget(self.highCutSlider)

        mappingLayout.addWidget(self.highCutLabel, row, 0)
        mappingLayout.addWidget(highCutFieldWidget, row, 1)

        self.highCutSlider.valueChanged.connect(self.onHighCutoffChanged)
        self.highCutSlider.enabled = currentMode == "Voltage Mapping"

        row += 1

        # ----------------------------------------------------------------------
        # Voltage Low Cutoff
        # ----------------------------------------------------------------------
        self.lowCutLabel = qt.QLabel("Low Cutoff [mV]:")
        self.lowCutLabel.setStyleSheet("font-weight: normal; color: #CCCCCC; margin-left: 12px;")

        self.lowCutSlider = ctk.ctkSliderWidget()
        self.lowCutSlider.singleStep = 0.01
        self.lowCutSlider.minimum = 0.0
        self.lowCutSlider.maximum = 2.0
        self.lowCutSlider.decimals = 3
        self.lowCutSlider.value = getattr(self.pn, "voltageLowCutoff", 0.1)

        lowCutFieldWidget = qt.QWidget()
        lowCutFieldLayout = qt.QHBoxLayout(lowCutFieldWidget)
        lowCutFieldLayout.setContentsMargins(0, 0, 0, 0)
        lowCutFieldLayout.setSpacing(0)
        lowCutFieldLayout.addWidget(self.lowCutSlider)

        mappingLayout.addWidget(self.lowCutLabel, row, 0)
        mappingLayout.addWidget(lowCutFieldWidget, row, 1)

        self.lowCutSlider.valueChanged.connect(self.onLowCutoffChanged)
        self.lowCutSlider.enabled = currentMode == "Voltage Mapping"

        row += 1

        # ----------------------------------------------------------------------
        # Distance Threshold
        # ----------------------------------------------------------------------
        self.distanceLabel = qt.QLabel("Distance Threshold [mm]:")
        self.distanceLabel.setStyleSheet("font-weight: normal; color: #CCCCCC; margin-left: 12px;")

        self.distanceSlider = ctk.ctkSliderWidget()
        self.distanceSlider.singleStep = 0.5
        self.distanceSlider.minimum = 1.0
        self.distanceSlider.maximum = 20.0
        self.distanceSlider.decimals = 1
        self.distanceSlider.value = getattr(self.pn, "cartoDistanceThresholdMm", 7.0)

        distanceFieldWidget = qt.QWidget()
        distanceFieldLayout = qt.QHBoxLayout(distanceFieldWidget)
        distanceFieldLayout.setContentsMargins(0, 0, 0, 0)
        distanceFieldLayout.setSpacing(0)
        distanceFieldLayout.addWidget(self.distanceSlider)

        mappingLayout.addWidget(self.distanceLabel, row, 0)
        mappingLayout.addWidget(distanceFieldWidget, row, 1)

        self.distanceSlider.valueChanged.connect(self.onDistanceThresholdChanged)
        self.distanceSlider.enabled = currentMode == "Voltage Mapping"

        row += 1

        # ----------------------------------------------------------------------
        # Sharpness
        # ----------------------------------------------------------------------
        self.sharpnessLabel = qt.QLabel("Sharpness:")
        self.sharpnessLabel.setStyleSheet("font-weight: normal; color: #CCCCCC; margin-left: 12px;")

        self.sharpnessSlider = ctk.ctkSliderWidget()
        self.sharpnessSlider.singleStep = 0.1
        self.sharpnessSlider.minimum = 0.5
        self.sharpnessSlider.maximum = 6.0
        self.sharpnessSlider.decimals = 1
        self.sharpnessSlider.value = getattr(self.pn, "cartoGaussianSharpness", 3.0)

        sharpnessFieldWidget = qt.QWidget()
        sharpnessFieldLayout = qt.QHBoxLayout(sharpnessFieldWidget)
        sharpnessFieldLayout.setContentsMargins(0, 0, 0, 0)
        sharpnessFieldLayout.setSpacing(0)
        sharpnessFieldLayout.addWidget(self.sharpnessSlider)

        mappingLayout.addWidget(self.sharpnessLabel, row, 0)
        mappingLayout.addWidget(sharpnessFieldWidget, row, 1)

        self.sharpnessSlider.valueChanged.connect(self.onSharpnessChanged)
        self.sharpnessSlider.enabled = currentMode == "Voltage Mapping"

        row += 1

        # ----------------------------------------------------------------------
        # Mapping Point Labels
        # ----------------------------------------------------------------------
        labelHeader = qt.QLabel("Mapping Point Labels:")
        labelHeader.setStyleSheet("font-weight: bold; color: #CCCCCC; margin-top: 4px;")
        self.applyOpticalLeftAlignment(labelHeader)

        mappingLayout.addWidget(labelHeader, row, 0)

        self.btnToggleLabels = qt.QPushButton("Hide Labels")
        self.btnToggleLabels.setCheckable(True)
        self.btnToggleLabels.setChecked(True)
        self.btnToggleLabels.setStyleSheet(toggleStyle)

        w = max(
            self.prePhaseRadio.sizeHint.width(),
            self.postPhaseRadio.sizeHint.width(),
        )
        self.btnToggleLabels.setFixedWidth(w)

        self.lblMappingLabelsState = qt.QLabel("(Visible: ON)")
        self.lblMappingLabelsState.setStyleSheet("color: #CCCCCC; margin-left: 6px;")
        self.lblMappingLabelsState.setFixedWidth(74)

        stateWidget = qt.QWidget()
        stateLayout = qt.QHBoxLayout(stateWidget)
        stateLayout.setContentsMargins(0, 0, 0, 0)
        stateLayout.setSpacing(0)
        stateLayout.addWidget(self.btnToggleLabels)
        stateLayout.addSpacing(12)
        stateLayout.addWidget(self.lblMappingLabelsState)
        stateLayout.addStretch(1)

        mappingLayout.addWidget(stateWidget, row, 1)

        self.btnToggleLabels.clicked.connect(self.onToggleMappingLabels)

        # ----------------------------------------------------------------------
        # Normalize label widths (geometric alignment)
        # ----------------------------------------------------------------------
        labelWidgets = [
            modeLabel,
            mappingPhaseLabel,
            self.voltageHeader,
            self.highCutLabel,
            self.lowCutLabel,
            self.distanceLabel,
            self.sharpnessLabel,
            labelHeader,
        ]
        maxLabelWidth = max(lbl.sizeHint.width() for lbl in labelWidgets)
        for lbl in labelWidgets:
            lbl.setFixedWidth(maxLabelWidth)

        # ============================================================
        # Point Management
        # ============================================================
        self.mgmtCollapsibleButton = ctk.ctkCollapsibleButton()
        self.mgmtCollapsibleButton.text = "Point Management"
        self.mgmtCollapsibleButton.collapsed = True
        layout.addWidget(self.mgmtCollapsibleButton)

        mgmtContainer = qt.QWidget()
        mgmtLayout = qt.QVBoxLayout(mgmtContainer)
        self.mgmtCollapsibleButton.setLayout(qt.QVBoxLayout())
        self.mgmtCollapsibleButton.layout().addWidget(mgmtContainer)

        mgmtLayout.setContentsMargins(8, 4, 8, 8)
        mgmtLayout.setSpacing(6)

        self.btnDeleteLastMapping = qt.QPushButton("Delete Last Mapping Point")
        self.btnDeleteLastMapping.clicked.connect(self.onDeleteLastMappingPoint)
        mgmtLayout.addWidget(self.btnDeleteLastMapping)

        self.btnDeleteLastAblation = qt.QPushButton("Delete Last Ablation Point")
        self.btnDeleteLastAblation.clicked.connect(self.onDeleteLastAblationPoint)
        mgmtLayout.addWidget(self.btnDeleteLastAblation)

        self.btnClearMapping = qt.QPushButton("Clear Mapping Points")
        self.btnClearMapping.clicked.connect(self.onClearMappingPoints)
        mgmtLayout.addWidget(self.btnClearMapping)

        self.btnClearAblation = qt.QPushButton("Clear Ablation Points")
        self.btnClearAblation.clicked.connect(self.onClearAblationPoints)
        mgmtLayout.addWidget(self.btnClearAblation)

        # self.restoreBackupButton = qt.QPushButton("🔄 Restore Backup…")
        self.restoreBackupButton = qt.QPushButton("\U0001f504 Restore Backup…")
        self.restoreBackupButton.setFixedSize(160, 28)
        self.restoreBackupButton.clicked.connect(self.onRestoreBackupClicked)
        mgmtLayout.addWidget(self.restoreBackupButton)

        layout.addStretch(1)

        # ============================================================
        # Ensure markups nodes exist
        # ============================================================
        if not self.pn.ablationPts:
            self.pn.ablationPts = slicer.mrmlScene.AddNewNodeByClass("vtkMRMLMarkupsFiducialNode", "ablationPts")

        if not self.pn.mappingPts:
            self.pn.mappingPts = slicer.mrmlScene.AddNewNodeByClass("vtkMRMLMarkupsFiducialNode", "mappingPts")

        self.attachPointObservers()

        # ----------------------------------------------------------------------
        # Enforce initial mutual exclusivity based on current mapping mode
        # ----------------------------------------------------------------------
        self.onMappingModeChanged(self.pn.mappingMode)

        # ----------------------------------------------------------------------
        # Removed: forced dock/widget sizePolicy overrides that expanded the panel
        # ----------------------------------------------------------------------
        # The following was removed:
        #
        # mw = slicer.util.mainWindow()
        # panel = mw.findChild(qt.QWidget, "PanelDockWidget")
        # if panel:
        #     panel.setSizePolicy(qt.QSizePolicy.Minimum, qt.QSizePolicy.Expanding)
        #     panel.setMinimumWidth(0)
        #     panel.setMaximumWidth(430)
        #
        # for cb in self.findChildren(ctk.ctkCollapsibleButton):
        #     cb.setSizePolicy(qt.QSizePolicy.Minimum, qt.QSizePolicy.Preferred)
        #     cb.setMinimumWidth(0)
        #
        # for w in self.findChildren(qt.QWidget):
        #     w.setSizePolicy(qt.QSizePolicy.Minimum, qt.QSizePolicy.Preferred)
        #
        # These overrides caused QComboBox and other widgets to stretch and
        # unnecessarily expand the side panel. Qt's default size policies are
        # now preserved for a more natural, professional layout.

    # ----------------------------------------------------------------------
    # MAPPING PHASE CHANGE
    # ----------------------------------------------------------------------
    def onMappingPhaseChanged(self, checked):
        if not checked:
            return
        phase = "PRE" if self.prePhaseRadio.isChecked() else "POST"
        self.pn.mappingPhase = phase

    # ----------------------------------------------------------------------
    # MAPPING MODE CHANGE
    # ----------------------------------------------------------------------
    def onMappingModeChanged(self, mode: str):
        """
        Handle user-initiated mapping-mode changes.

        Responsibilities:
          - Update EPCMRParameterNode.mappingMode
          - Enable/disable voltage-specific UI controls
          - Show/hide Activation header + info + divider (CARTO-style)
          - Show/hide Voltage header + controls (mutually exclusive)
          - Reset mapping/ablation points
          - Reset RA mesh to neutral baseline
          - Trigger SceneManager colormap update
          - Force deterministic 3D view refresh (fixes Voltage->Activation visibility issue)
        """

        # Mark mode switch as in progress so MappingEventController
        # ignores transient markups events (mappingPts clearing, etc.).
        self.pn.modeSwitchInProgress = True

        # Update mapping mode on the parameter node
        self.pn.mappingMode = mode

        # Determine mode
        isVoltage = mode == "Voltage Mapping"

        # ------------------------------------------------------------------
        # Voltage-specific UI controls (dual-knob compression)
        # ------------------------------------------------------------------
        if not isVoltage:
            # Disable voltage controls in Activation Time Mapping
            self.highCutSlider.enabled = False
            self.lowCutSlider.enabled = False
            if hasattr(self, "distanceSlider"):
                self.distanceSlider.enabled = False
            if hasattr(self, "sharpnessSlider"):
                self.sharpnessSlider.enabled = False

        else:
            # Enable voltage controls
            self.highCutSlider.enabled = True
            self.lowCutSlider.enabled = True
            if hasattr(self, "distanceSlider"):
                self.distanceSlider.enabled = True
            if hasattr(self, "sharpnessSlider"):
                self.sharpnessSlider.enabled = True

            # Initialize high cutoff
            if getattr(self.pn, "voltageHighCutoff", None) is None:
                self.pn.voltageHighCutoff = float(self.highCutSlider.value)
            else:
                self.highCutSlider.value = float(self.pn.voltageHighCutoff)

            # Initialize low cutoff
            if getattr(self.pn, "voltageLowCutoff", None) is None:
                self.pn.voltageLowCutoff = 0.1
            else:
                self.lowCutSlider.value = float(self.pn.voltageLowCutoff)

        # ------------------------------------------------------------------
        # Activation header + info + divider visibility (CARTO-faithful)
        # ------------------------------------------------------------------
        if hasattr(self, "activationHeader"):
            self.activationHeader.setVisible(not isVoltage)
        if hasattr(self, "activationInfo"):
            self.activationInfo.setVisible(not isVoltage)
        if hasattr(self, "activationDivider"):
            self.activationDivider.setVisible(not isVoltage)

        # ------------------------------------------------------------------
        # Voltage header + controls visibility (mutually exclusive)
        # ------------------------------------------------------------------
        if hasattr(self, "voltageHeader"):
            self.voltageHeader.setVisible(isVoltage)

        # Voltage labels visibility (mutually exclusive)
        if hasattr(self, "highCutLabel"):
            self.highCutLabel.setVisible(isVoltage)
        if hasattr(self, "lowCutLabel"):
            self.lowCutLabel.setVisible(isVoltage)
        if hasattr(self, "distanceLabel"):
            self.distanceLabel.setVisible(isVoltage)
        if hasattr(self, "sharpnessLabel"):
            self.sharpnessLabel.setVisible(isVoltage)

        # Hide/show voltage sliders as a group
        self.highCutSlider.setVisible(isVoltage)
        self.lowCutSlider.setVisible(isVoltage)
        if hasattr(self, "distanceSlider"):
            self.distanceSlider.setVisible(isVoltage)
        if hasattr(self, "sharpnessSlider"):
            self.sharpnessSlider.setVisible(isVoltage)

        # ------------------------------------------------------------------
        # Reset mapping state (mappingPts cleared, RA mesh reset to neutral)
        # ------------------------------------------------------------------
        self._resetMappingPointsForModeSwitch()

        # ------------------------------------------------------------------
        # Clear ablationPts
        # ------------------------------------------------------------------
        abl = getattr(self.pn, "ablationPts", None)
        if abl:
            abl.RemoveAllControlPoints()

        # ------------------------------------------------------------------
        # Trigger recompute (SceneManager handles legend)
        # ------------------------------------------------------------------
        if hasattr(self.logic, "sceneManager") and self.logic.sceneManager:
            self.logic.sceneManager.updateRightAtrialColormap()

        # ------------------------------------------------------------------
        # Force deterministic 3D view refresh
        # Ensures RA model visibility is correct after mode switch
        # ------------------------------------------------------------------
        self._force3DViewRefresh()

        # ------------------------------------------------------------------
        # Status message
        # ------------------------------------------------------------------
        slicer.util.showStatusMessage(
            f"Mapping mode switched to {mode}. All mapping points and colormap reset.",
            3000,
        )

        # ------------------------------------------------------------------
        # Clear modeSwitchInProgress after observers have settled
        # ------------------------------------------------------------------
        qt.QTimer.singleShot(
            50,
            lambda: setattr(self.pn, "modeSwitchInProgress", False),
        )

    # ----------------------------------------------------------------------
    # VOLTAGE HIGH CUTOFF CHANGE  (upper bound of dual-compression)
    # ----------------------------------------------------------------------
    def onHighCutoffChanged(self, value):
        """
        Handle changes to the *high* voltage cutoff (upper bound) used in
        CARTO-style dual-compression voltage mapping.

        This function is the logical counterpart to onLowCutoffChanged():
            - onLowCutoffChanged() adjusts the *lower* bound
            - onHighCutoffChanged() adjusts the *upper* bound

        Both functions:
            - operate only in Voltage Mapping mode
            - update the parameter node
            - re-run interpolation
            - force a legend update
        """

        # Only active in Voltage Mapping
        if getattr(self.pn, "mappingMode", "Activation Time Mapping") != "Voltage Mapping":
            return

        # Store high cutoff in mV
        self.pn.voltageHighCutoff = float(value)

        # Ensure low cutoff exists (dual-compression requires both ends)
        if getattr(self.pn, "voltageLowCutoff", None) is None:
            self.pn.voltageLowCutoff = 0.1  # default low cutoff

        node = getattr(self.pn, "mappingPts", None)
        if node and node.GetNumberOfControlPoints() >= 2:
            # Re-run interpolation so dual compression is applied
            self.logic.modelObserver.interpolator.run(self.pn.mappingPts)

            # Force legend update (upper bound changed)
            if hasattr(self.logic, "sceneManager") and self.logic.sceneManager:
                self.logic.sceneManager.updateRightAtrialColormap()

    # ----------------------------------------------------------------------
    # VOLTAGE LOW CUTOFF CHANGE  (lower bound of dual-compression)
    # ----------------------------------------------------------------------
    def onLowCutoffChanged(self, value):
        """
        Handle changes to the *low* voltage cutoff (lower bound) used in
        CARTO-style dual-compression voltage mapping.

        This function is the logical counterpart to onHighCutoffChanged():
            - onHighCutoffChanged() adjusts the *upper* bound
            - onLowCutoffChanged() adjusts the *lower* bound

        Both functions:
            - operate only in Voltage Mapping mode
            - update the parameter node
            - re-run interpolation
            - force a legend update
        """

        # Only active in Voltage Mapping
        if getattr(self.pn, "mappingMode", "Activation Time Mapping") != "Voltage Mapping":
            return

        # Store low cutoff in mV
        self.pn.voltageLowCutoff = float(value)

        # Ensure high cutoff exists (dual-compression requires both ends)
        if getattr(self.pn, "voltageHighCutoff", None) is None:
            self.pn.voltageHighCutoff = float(self.highCutSlider.value)

        node = getattr(self.pn, "mappingPts", None)
        if node and node.GetNumberOfControlPoints() >= 2:
            # Re-run interpolation so dual compression is applied
            self.logic.modelObserver.interpolator.run(self.pn.mappingPts)

            # Force legend update (lower bound changed)
            if hasattr(self.logic, "sceneManager") and self.logic.sceneManager:
                self.logic.sceneManager.updateRightAtrialColormap()

    # ----------------------------------------------------------------------
    # VOLTAGE DISTANCE THRESHOLD CHANGE (Gaussian radius in mm)
    # ----------------------------------------------------------------------
    def onDistanceThresholdChanged(self, value):
        """
        Handle changes to the CARTO-style distance threshold (Gaussian radius in mm)
        used for voltage smoothing on the RA surface.

        Only active in Voltage Mapping:
            - updates parameter node
            - re-runs interpolation
            - lets SceneManager handle legend update
        """
        if getattr(self.pn, "mappingMode", "Activation Time Mapping") != "Voltage Mapping":
            return

        self.pn.cartoDistanceThresholdMm = float(value)

        node = getattr(self.pn, "mappingPts", None)
        if node and node.GetNumberOfControlPoints() >= 2:
            self.logic.modelObserver.interpolator.run(self.pn.mappingPts)

    # ----------------------------------------------------------------------
    # VOLTAGE SHARPNESS CHANGE (Gaussian falloff)
    # ----------------------------------------------------------------------
    def onSharpnessChanged(self, value):
        """
        Handle changes to the CARTO-style Gaussian sharpness parameter
        controlling local falloff inside the distance threshold.

        Only active in Voltage Mapping:
            - updates parameter node
            - re-runs interpolation
            - lets SceneManager handle legend update
        """
        if getattr(self.pn, "mappingMode", "Activation Time Mapping") != "Voltage Mapping":
            return

        self.pn.cartoGaussianSharpness = float(value)

        node = getattr(self.pn, "mappingPts", None)
        if node and node.GetNumberOfControlPoints() >= 2:
            self.logic.modelObserver.interpolator.run(self.pn.mappingPts)

    # ----------------------------------------------------------------------
    # MODE SWITCH RESET
    # ----------------------------------------------------------------------
    def _resetMappingPointsForModeSwitch(self):
        node = getattr(self.pn, "mappingPts", None)
        if not node:
            return

        slicer.mrmlScene.StartState(slicer.vtkMRMLScene.BatchProcessState)
        try:
            node.RemoveAllControlPoints()
            self.resetMeshColors()
        finally:
            slicer.mrmlScene.EndState(slicer.vtkMRMLScene.BatchProcessState)

    # ----------------------------------------------------------------------
    # POINT INTERACTION HANDLERS
    # ----------------------------------------------------------------------
    def _onPointStartInteraction(self, caller, event):
        """
        Start of user interaction with a control point.

        NEW:
          - Suppressed during backup restore to avoid false state changes.
        """
        # Suppress during restore
        if getattr(self.logic.modelObserver, "isRestoringBackup", False):
            return

        dn = caller.GetDisplayNode()
        idx = dn.GetActiveControlPoint() if dn else -1
        if idx < 0:
            self._lastSelectedPointInfo = None
            return

        pos = [0, 0, 0]
        caller.GetNthControlPointPosition(idx, pos)
        label = caller.GetNthControlPointLabel(idx)
        self._lastSelectedPointInfo = (pos, label)

    def _onPointEndInteraction(self, caller, event):
        """
        Called when user finishes moving/placing a point.

        NEW:
          - Suppressed during backup restore.
          - Prevents legend updates and interpolation storms during restore.
        """
        # Suppress during restore
        if getattr(self.logic.modelObserver, "isRestoringBackup", False):
            return

        if caller == getattr(self.pn, "mappingPts", None):
            if caller.GetNumberOfControlPoints() >= 2:
                if hasattr(self.logic, "sceneManager") and self.logic.sceneManager:
                    self.logic.sceneManager.updateRightAtrialColormap()

    def _onPointRemoved(self, caller, event):
        """
        Called when a point is removed.

        NEW:
          - Suppressed during backup restore.
          - Prevents deletedPointsStack pollution during restore.
        """
        # Suppress during restore
        if getattr(self.logic.modelObserver, "isRestoringBackup", False):
            return

        if self._isProgrammaticDelete:
            return

        pos = None
        label = None

        if self._lastSelectedPointInfo:
            pos, label = self._lastSelectedPointInfo
        else:
            dn = caller.GetDisplayNode()
            idx = dn.GetActiveControlPoint() if dn else -1
            if idx < 0:
                return
            pos = [0, 0, 0]
            caller.GetNthControlPointPosition(idx, pos)
            label = caller.GetNthControlPointLabel(idx)

        targetName = "mappingPts" if caller == getattr(self.pn, "mappingPts", None) else "ablationPts"
        self.deletedPointsStack.append((pos, label, targetName))
        self._lastSelectedPointInfo = None

    def onToggleMappingLabels(self):
        """
        Toggle mappingPts label visibility.

        UX grammar:
            - PRESSED   = labels visible
            - UNPRESSED = labels hidden
            - Button text always shows the action the user can take
            - State label reflects the current visibility state

        NEW:
          - Suppressed during backup restore to avoid UI flicker.
        """

        # Suppress during restore
        if getattr(self.logic.modelObserver, "isRestoringBackup", False):
            return

        node = getattr(self.pn, "mappingPts", None)
        if not node:
            return

        dn = node.GetDisplayNode()
        if not dn:
            return

        if self.btnToggleLabels.isChecked():
            # PRESSED = labels visible
            dn.SetPointLabelsVisibility(True)
            self.btnToggleLabels.setText("Hide Labels")
            self.lblMappingLabelsState.setText("(Visible: ON)")
        else:
            # UNPRESSED = labels hidden
            dn.SetPointLabelsVisibility(False)
            self.btnToggleLabels.setText("Show Labels")
            self.lblMappingLabelsState.setText("(Visible: OFF)")

        slicer.util.forceRenderAllViews()

    # ----------------------------------------------------------------------
    # DELETE / CLEAR BUTTONS (B3)
    # ----------------------------------------------------------------------
    def onDeleteLastMappingPoint(self):
        node = getattr(self.pn, "mappingPts", None)
        if not node or node.GetNumberOfControlPoints() == 0:
            return

        lastIdx = node.GetNumberOfControlPoints() - 1
        pos = [0, 0, 0]
        node.GetNthControlPointPosition(lastIdx, pos)
        label = node.GetNthControlPointLabel(lastIdx)

        self.deletedPointsStack.append((pos, label, "mappingPts"))

        self._isProgrammaticDelete = True
        node.RemoveNthControlPoint(lastIdx)
        self._isProgrammaticDelete = False

        self.logic.modelObserver.interpolator.run(self.pn.mappingPts)

    def onDeleteLastAblationPoint(self):
        node = getattr(self.pn, "ablationPts", None)
        if not node or node.GetNumberOfControlPoints() == 0:
            return

        lastIdx = node.GetNumberOfControlPoints() - 1
        pos = [0, 0, 0]
        node.GetNthControlPointPosition(lastIdx, pos)
        label = node.GetNthControlPointLabel(lastIdx)

        self.deletedPointsStack.append((pos, label, "ablationPts"))

        self._isProgrammaticDelete = True
        node.RemoveNthControlPoint(lastIdx)
        self._isProgrammaticDelete = False

    def onClearMappingPoints(self):
        node = getattr(self.pn, "mappingPts", None)
        if not node or node.GetNumberOfControlPoints() == 0:
            return

        if not slicer.util.confirmOkCancelDisplay("Delete all mapping points"):
            return

        slicer.mrmlScene.StartState(slicer.vtkMRMLScene.BatchProcessState)
        try:
            node.RemoveAllControlPoints()
            self.resetMeshColors()
        finally:
            slicer.mrmlScene.EndState(slicer.vtkMRMLScene.BatchProcessState)

        self.deletedPointsStack = []

    def onClearAblationPoints(self):
        node = getattr(self.pn, "ablationPts", None)
        if not node or node.GetNumberOfControlPoints() == 0:
            return

        if not slicer.util.confirmOkCancelDisplay("Delete all ablation points"):
            return

        slicer.mrmlScene.StartState(slicer.vtkMRMLScene.BatchProcessState)
        try:
            node.RemoveAllControlPoints()
        finally:
            slicer.mrmlScene.EndState(slicer.vtkMRMLScene.BatchProcessState)

        self.deletedPointsStack = []

    # ----------------------------------------------------------------------
    # BACKUP RESTORE (A2 -- rewritten for new architecture)
    # ----------------------------------------------------------------------
    def onRestoreBackupClicked(self):
        """
        Opens a file dialog and restores a .mrk.json backup.

        Fully rewritten for new architecture:
          - No ghost nodes
          - No SubjectHierarchy warnings
          - No backup storms
          - No race conditions
        """
        startDir = self.pn.lastSavePath or os.path.expanduser("~")

        dialog = qt.QFileDialog(self, "Select Backup File", startDir, "Markup Backup (*.mrk.json)")
        dialog.setFileMode(qt.QFileDialog.ExistingFile)
        dialog.setOption(qt.QFileDialog.DontUseNativeDialog, True)
        dialog.resize(900, 400)

        if dialog.exec_():
            selected = dialog.selectedFiles()
            if not selected:
                return
            filePath = selected[0]
        else:
            return

        # Defer actual restore to next event loop cycle
        qt.QTimer.singleShot(0, lambda: self._doRestoreBackup(filePath))

    def _doRestoreBackup(self, filePath):
        """
        Clean, restore-safe backup restore:

          - Loads backup into a temporary markups node
          - Copies points into the persistent node (numeric labels enforced)
          - Keeps the temp node inert (hidden, not saved) instead of deleting it
          - Triggers a single final interpolation if mappingPts
          - Suppresses MappingEventController + backups + legend updates during restore
          - Uses MRML BatchProcessState to avoid SubjectHierarchy inconsistencies
        """
        if type(self.pn).__name__ != "EPCMRParameterNode":
            raise RuntimeError("_doRestoreBackup() expected wrapped EPCMRParameterNode")

        markupsLogic = slicer.modules.markups.logic()
        tempNodeID = markupsLogic.LoadMarkupsFromJson(filePath)
        if not tempNodeID:
            slicer.util.errorDisplay(f"Failed to load: {filePath}")
            return

        tempNode = slicer.mrmlScene.GetNodeByID(tempNodeID)
        if not tempNode:
            slicer.util.errorDisplay("Backup loaded but node not found in scene.")
            return

        baseName = tempNode.GetName().split("_")[0]
        persistentNode = getattr(self.pn, baseName, None)
        if not persistentNode:
            slicer.util.errorDisplay(f"No persistent node found for {baseName}")
            return

        # ------------------------------------------------------------------
        # Helper: enforce numeric labels (no warnings, no ValueError)
        # ------------------------------------------------------------------
        def sanitize_label(raw, fallback="0.0"):
            if raw is None:
                return fallback
            s = raw.strip().replace(",", ".")
            try:
                float(s)
                return s
            except Exception:
                return fallback

        # ------------------------------------------------------------------
        # Suppress MappingEventController + backups + legend updates
        # ------------------------------------------------------------------
        mec = getattr(self.logic, "modelObserver", None)
        sceneManager = getattr(self.logic, "sceneManager", None)

        if mec:
            mec.isRestoringBackup = True
        if sceneManager:
            sceneManager.isRestoringBackup = True
            sceneManager.suppressBackup = True

        # Batch scene changes to keep SubjectHierarchy consistent
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

                # Copy points with numeric-only labels
                for i in range(tempNode.GetNumberOfControlPoints()):
                    pos = [0, 0, 0]
                    tempNode.GetNthControlPointPosition(i, pos)
                    raw_label = tempNode.GetNthControlPointLabel(i)
                    clean_label = sanitize_label(raw_label)

                    idx = persistentNode.AddControlPoint(pos)
                    persistentNode.SetNthControlPointLabel(idx, clean_label)

                slicer.modules.markups.logic().SetActiveListID(persistentNode)

            finally:
                # Do NOT remove the temporary node:
                #   - hide it
                #   - prevent saving with scene
                # This avoids SubjectHierarchy warnings and cache inconsistencies.
                try:
                    tempNode.SetDisplayVisibility(False)
                    tempNode.SetSaveWithScene(False)
                except Exception as e:
                    logging.warning(f"RAFlutterWidget: Failed to inactivate temp backup node: {e}")

                slicer.mrmlScene.EndState(slicer.vtkMRMLScene.BatchProcessState)

                # Re-enable observers / restore flags
                if mec:
                    mec.isRestoringBackup = False
                if sceneManager:
                    sceneManager.isRestoringBackup = False
                    sceneManager.suppressBackup = False

                # ----------------------------------------------------------
                # Schedule a single FINAL interpolation + legend update
                # ----------------------------------------------------------
                if baseName == "mappingPts":

                    def finalize_restore():
                        node = persistentNode
                        if not node or node.GetNumberOfControlPoints() < 2:
                            return

                        # One final interpolation
                        try:
                            if hasattr(self.logic, "modelObserver") and self.logic.modelObserver:
                                self.logic.modelObserver.interpolator.run(node)
                        except Exception as e:
                            logging.error(f"Interpolation after restore failed: {e}")

                        # One final legend update
                        try:
                            if hasattr(self.logic, "sceneManager") and self.logic.sceneManager:
                                self.logic.sceneManager.updateRightAtrialColormap()
                        except Exception as e:
                            logging.error(f"Legend update after restore failed: {e}")

                        slicer.util.forceRenderAllViews()

                    qt.QTimer.singleShot(0, finalize_restore)

        qt.QTimer.singleShot(0, doRestore)

    # ----------------------------------------------------------------------
    # REPLAYER
    # ----------------------------------------------------------------------
    def onLaunchReplayer(self):
        """
        Standardized shared replayer access using the NEW lazy-replayer architecture.

        IMPORTANT:
        ----------
        RAFlutterWidget no longer accesses mainWidget.replayer.
        Instead, it calls self.getReplayer() which:
          - creates the replayer if needed
          - returns the existing instance otherwise
        """

        # Guard: ensure we actually have a factory
        if not self.getReplayer:
            logging.error("RAFlutterWidget: getReplayer() not provided.")
            # Reset toggle, since we cannot open anything
            if hasattr(self, "launchReplayerButton"):
                self.launchReplayerButton.setChecked(False)
            return

        # Lazy creation or retrieval
        self.replayer = self.getReplayer()
        if not self.replayer:
            logging.error("RAFlutterWidget: Replayer could not be created.")
            if hasattr(self, "launchReplayerButton"):
                self.launchReplayerButton.setChecked(False)
            return

        # Toggle visibility based on button state
        if self.launchReplayerButton.isChecked():
            # Ensure the window is recreated if it was previously closed/nullified.
            replayer_ui = self.replayer.show_ui()

            # Safely attach the sync callback to the newly created/retrieved UI
            # This calls back to EPCMRWidget.syncReplayerButtons() to reset the toggle UI
            if self.mainWidget and hasattr(self.mainWidget, "syncReplayerButtons"):
                replayer_ui.on_closed_callback = self.mainWidget.syncReplayerButtons

            replayer_ui.show()

            # --- POSITIONING LOGIC (FORCE-FLUSH LEFT) ---
            desktop = qt.QApplication.desktop()
            screenIndex = desktop.screenNumber(slicer.util.mainWindow())
            screenRect = desktop.availableGeometry(screenIndex)

            windowSize = replayer_ui.sizeHint
            w = windowSize.width()
            h = windowSize.height()

            # Positions the replayer window at the bottom left
            posX = screenRect.left() + 2
            posY = screenRect.bottom() - h - 30

            replayer_ui.setGeometry(posX, posY, w, h)

            # --- FRONT & FOCUS LOGIC ---
            replayer_ui.raise_()
            replayer_ui.activateWindow()
            replayer_ui.setFocus()

            self.launchReplayerButton.text = "Hide Replayer Controls"
        else:
            # Safely hide only if the UI exists
            if hasattr(self.replayer, "ui") and self.replayer.ui:
                self.replayer.ui.hide()
            self.launchReplayerButton.text = "Show Replayer Controls"

    # ----------------------------------------------------------------------
    # MESH RESET
    # ----------------------------------------------------------------------
    def resetMeshColors(self):
        """
        Reset RA clone to neutral (no scalar visibility, no interpolated scalar array).
        Uses RACloneManager via ModelObserver facade.
        """
        try:
            # Resolve main logic / modelObserver
            if not hasattr(self.mainLogic, "modelObserver") or not self.mainLogic.modelObserver:
                if hasattr(slicer.modules, "epcmr"):
                    mainLogic = slicer.modules.epcmr.logic()
                    obs = mainLogic.modelObserver if hasattr(mainLogic, "modelObserver") else None
                else:
                    obs = None
            else:
                obs = self.mainLogic.modelObserver

            if not obs:
                return

            # New architecture: RACloneManager is typically exposed as 'raCloneManager'
            ra_manager = getattr(obs, "raCloneManager", None)
            if not ra_manager:
                # Backward-compatible fallback name
                ra_manager = getattr(obs, "raManager", None)

            if not ra_manager:
                return

            cloned = getattr(ra_manager, "clonedRA", None)
            if not cloned:
                return

            dn = cloned.GetDisplayNode()
            if dn:
                dn.SetScalarVisibility(False)
                dn.SetActiveScalar("", vtk.vtkAssignAttribute.POINT_DATA)

            polyData = cloned.GetPolyData()
            if polyData:
                pd = polyData.GetPointData()
                if pd:
                    # Remove any mapping-related arrays if present
                    for arrName in ["ActivationTime", "Voltage", "InterpolatedData"]:
                        if pd.GetArray(arrName):
                            pd.RemoveArray(arrName)
                polyData.Modified()

            cloned.Modified()
            slicer.util.forceRenderAllViews()
            logging.info("RAFlutterWidget: RA Mesh colors reset to neutral.")
        except Exception as e:
            logging.error(f"RAFlutterWidget: Failed to reset mesh colors: {e}")

    def _force3DViewRefresh(self):
        """
        Force a deterministic VTK render after scene changes.
        Required because switching Voltage -> Activation removes scalar arrays,
        causing VTK to skip rendering until the user interacts (zoom/pan).
        """
        lm = slicer.app.layoutManager()
        if not lm:
            return

        threeDWidget = lm.threeDWidget(0)
        if not threeDWidget:
            return

        view = threeDWidget.threeDView()
        if not view:
            return

        # Access the underlying VTK renderer
        renderer = view.renderWindow().GetRenderers().GetFirstRenderer()
        if renderer:
            renderer.ResetCameraClippingRange()

        # Force render on next event loop cycle
        qt.QTimer.singleShot(0, view.forceRender)

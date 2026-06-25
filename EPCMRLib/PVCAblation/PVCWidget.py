# ruff: isort: skip_file

# ---- Core "V-Q-C-S" Stack (Mnemonic: Visualize Quickly Configure Slicer) ----
import vtk  # Visualize (the data) # noqa: F401
import qt  # Quick (standard buttons)
import ctk  # Configure (medical widgets)
import slicer  # Slicer (the app)
import logging

# ---- Local Libs ----
from .PVCLogic import PVCLogic
from EPCMRLib.EPCMRParameterNode import EPCMRParameterNode


class PVCWidget(qt.QWidget):
    """
    PVC Ablation workflow UI.

    Updated for NEW EPCMR architecture:
      - Lazy replayer creation via getReplayer()
      - No direct CatheterReplayer imports
      - No dependency on mainWidget.replayer
      - Fully consistent with RAFlutterWidget
    """

    def __init__(self, logic, pNode: EPCMRParameterNode, mainWidget=None, parent=None, getReplayer=None):
        super().__init__(parent)

        # --- 1. Shared EPCMRLogic instance ---
        self.mainLogic = logic
        self.logic = PVCLogic()  # PVC-specific logic (unchanged)

        # --- 2. Wrapped EPCMRParameterNode ---
        self.pn = pNode
        if type(self.pn).__name__ != "EPCMRParameterNode":
            raise TypeError(f"PVCWidget expected wrapped EPCMRParameterNode, got {type(self.pn)}")

        # --- 3. Backlink to main EPCMRWidget ---
        self.mainWidget = mainWidget

        # --- 4. Lazy replayer factory (NEW) ---
        #
        # IMPORTANT:
        # ----------
        # PVCWidget no longer accesses mainWidget.replayer.
        # Instead, it receives a callable (logic.getReplayer) that will
        # create or return the replayer *lazily* when needed.
        #
        self.getReplayer = getReplayer

        # --- 5. Internal state ---
        self.replayer = None

        # --- 6. Build UI ---
        self.setup()

    def setup(self):
        # Use a local variable 'layout' to avoid QWidget naming conflicts
        layout = qt.QVBoxLayout(self)

        # --- Placeholder UI ---
        self.infoLabel = qt.QLabel("\n--- PVC Ablation Procedure ---\n")
        self.infoLabel.setAlignment(qt.Qt.AlignCenter)
        self.infoLabel.setStyleSheet("font-weight: bold; color: #CCCCCC; font-size: 14px;")
        layout.addWidget(self.infoLabel)

        self.descriptionLabel = qt.QLabel("This module will handle PVC mapping and site identification.")
        self.descriptionLabel.setWordWrap(True)
        self.descriptionLabel.setAlignment(qt.Qt.AlignCenter)
        layout.addWidget(self.descriptionLabel)

        # --- Catheter Replay Section ---
        self.replayerCollapsibleButton = ctk.ctkCollapsibleButton()
        self.replayerCollapsibleButton.text = "Catheter Replay"
        layout.addWidget(self.replayerCollapsibleButton)

        replayerFormLayout = qt.QFormLayout(self.replayerCollapsibleButton)

        self.launchReplayerButton = qt.QPushButton("Show Replayer Controls")
        self.launchReplayerButton.setCheckable(True)
        replayerFormLayout.addRow(self.launchReplayerButton)
        self.launchReplayerButton.clicked.connect(self.onLaunchReplayer)

        self.pvcButton = qt.QPushButton("Initialize PVC Mapping (Coming Soon)")
        self.pvcButton.setMinimumHeight(40)
        self.pvcButton.setEnabled(False)
        layout.addWidget(self.pvcButton)

        layout.addStretch(1)

    def onLaunchReplayer(self):
        """
        Standardized shared replayer access using the NEW lazy-replayer architecture.

        IMPORTANT:
        ----------
        PVCWidget no longer accesses mainWidget.replayer.
        Instead, it calls self.getReplayer() which:
          - creates the replayer if needed
          - returns the existing instance otherwise
        """

        if not self.getReplayer:
            logging.error("PVCWidget: getReplayer() not provided.")
            self.launchReplayerButton.setChecked(False)
            return

        # Lazy creation or retrieval
        self.replayer = self.getReplayer()
        if not self.replayer:
            logging.error("PVCWidget: Replayer could not be created.")
            self.launchReplayerButton.setChecked(False)
            return

        # Toggle visibility based on button state
        if self.launchReplayerButton.isChecked():
            replayer_ui = self.replayer.show_ui()

            # Attach sync callback to EPCMRWidget
            if hasattr(self.mainWidget, "syncReplayerButtons"):
                replayer_ui.on_closed_callback = self.mainWidget.syncReplayerButtons

            replayer_ui.show()

            # --- POSITIONING LOGIC (FORCE-FLUSH LEFT) ---
            desktop = qt.QApplication.desktop()
            screenIndex = desktop.screenNumber(slicer.util.mainWindow())
            screenRect = desktop.availableGeometry(screenIndex)

            windowSize = replayer_ui.sizeHint
            w = windowSize.width()
            h = windowSize.height()

            posX = screenRect.left() + 2
            posY = screenRect.bottom() - h - 30

            replayer_ui.setGeometry(posX, posY, w, h)

            replayer_ui.raise_()
            replayer_ui.activateWindow()
            replayer_ui.setFocus()

            self.launchReplayerButton.text = "Hide Replayer Controls"

        else:
            if hasattr(self.replayer, "ui") and self.replayer.ui:
                self.replayer.ui.hide()
            self.launchReplayerButton.text = "Show Replayer Controls"

    def onApply(self):
        """Placeholder for future PVC specific actions."""
        pass

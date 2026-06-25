# ------------------------------------------------------------------------------
#  MappingModeSelector
#  UI component for selecting the mapping mode used for right atrial colormap
#  computation (Shepard kernel interpolation).
#
#  This widget is workflow-agnostic and can be embedded inside any EPCMR
#  submodule (RAFlutter, PVCAblation, etc.) that supports mapping.
#
#  Responsibilities:
#    - Provide a deterministic UI element for selecting the mapping mode
#    - Update the EPCMRParameterNode when the user changes the mode
#    - Never perform any computation or scene manipulation
#
#  Notes:
#    - Logic/UI separation is preserved: this widget only updates the parameter
#      node; SceneManager or workflow logic reacts to the parameter change.
#    - MappingModeSelector must be a pure field widget: this widget now provides
#      only the "field" (combobox). The embedding widget (e.g. RAFlutterWidget)
#      provides the "Mapping Mode:" label in its own QFormLayout for
#      pixel-perfect alignment with other rows.
# ------------------------------------------------------------------------------

import qt


class MappingModeSelector(qt.QWidget):
    """Reusable UI component for selecting the mapping mode."""

    # Emits the new mode string whenever the user changes the selection
    # The signal must be defined as a class attribute, directly under the docstring, before __init__.
    mappingModeChanged = qt.Signal(str)

    def __init__(self, parameterNode, parent=None):
        super().__init__(parent)

        if parameterNode is None:
            raise RuntimeError("MappingModeSelector requires a valid parameter node")

        self._parameterNode = parameterNode

        # --- Layout ---
        # Pure "field" widget: only the combobox is owned here.
        # The parent form provides the label in its QFormLayout.
        layout = qt.QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        # --- Mapping Mode ComboBox ---
        self.mappingModeComboBox = qt.QComboBox()
        self.mappingModeComboBox.addItems(["Activation Time Mapping", "Voltage Mapping"])
        layout.addWidget(self.mappingModeComboBox)

        # --- Initialize from parameter node ---
        self._syncFromParameterNode()

        # --- Connect signals ---
        self.mappingModeComboBox.currentIndexChanged.connect(self._onMappingModeChanged)

    # --------------------------------------------------------------------------
    #  Internal: Sync UI from parameter node
    # --------------------------------------------------------------------------
    def _syncFromParameterNode(self) -> None:
        """Initialize the combo box based on the parameter node value."""
        mode = getattr(self._parameterNode, "mappingMode", None)

        if mode is None:
            # Default to Activation Time Mapping if not set
            self._parameterNode.mappingMode = "Activation Time Mapping"
            mode = "Activation Time Mapping"

        index = self.mappingModeComboBox.findText(mode)
        if index >= 0:
            self.mappingModeComboBox.setCurrentIndex(index)

    # --------------------------------------------------------------------------
    #  Internal: Handle user selection
    # --------------------------------------------------------------------------
    def _onMappingModeChanged(self, index: int) -> None:
        """Update the parameter node when the user selects a new mapping mode."""
        mode = self.mappingModeComboBox.currentText
        self._parameterNode.mappingMode = mode
        self.mappingModeChanged.emit(mode)

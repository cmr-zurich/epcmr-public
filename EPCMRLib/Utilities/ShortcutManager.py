import vtk  # noqa: I001
import qt
import ctk
import slicer
import logging
from typing import Any
from EPCMRLib.EPCMRParameterNode import EPCMRParameterNode


class ShortcutManager:
    def __init__(self, parentWindow, pNode: EPCMRParameterNode):
        # 1. Assign variables first
        self.parentWindow = parentWindow
        self.pNode = pNode
        self.shortcuts = {}
        self.transformNode_abl = None

        # 2. Setup the shortcuts now that self.parentWindow and self.shortcuts exist
        self.setupShortcuts()

    # ----------------------------------------------------------------------
    # PARAMETER NODE SYNC
    # ----------------------------------------------------------------------
    def syncParameterNode(self):
        """Re-fetches the ACTIVE ParameterNode from the module logic."""
        try:
            if hasattr(slicer.modules, "epcmr"):
                widget = slicer.modules.epcmr.widgetRepresentation().self()
                self.pNode = widget.logic.getParameterNode()

                isReplaying = getattr(self.pNode, "replayModeActive", False)
                if callable(isReplaying):
                    isReplaying = isReplaying()

                self.setEnabled(not isReplaying)

                if not isReplaying:
                    slicer.util.mainWindow().setFocus(qt.Qt.ActiveWindowFocusReason)
            else:
                self.setEnabled(False)

        except Exception as e:
            self.setEnabled(False)
            logging.debug(f"ShortcutManager: Failed to sync ParameterNode: {e}")

    # ----------------------------------------------------------------------
    # ENABLE / DISABLE SHORTCUTS
    # ----------------------------------------------------------------------
    def setEnabled(self, enabled):
        """Safe enabler that respects replay mode."""
        isReplaying = getattr(self.pNode, "replayModeActive", False)
        if callable(isReplaying):
            isReplaying = isReplaying()

        actual_state = False if isReplaying else enabled

        for shortcut in self.shortcuts.values():
            if shortcut:
                shortcut.setEnabled(actual_state)

        logging.info(f"ShortcutManager: State set to {actual_state}")

    # ----------------------------------------------------------------------
    # SHORTCUT CREATION
    # ----------------------------------------------------------------------
    def addShortcut(self, keySequence, callback):
        # Remove duplicates from previous reloads
        targetSequence = qt.QKeySequence(keySequence)
        targetString = targetSequence.toString()

        for child in self.parentWindow.findChildren(qt.QShortcut):
            try:
                if child.key.toString() == targetString:
                    child.setEnabled(False)
                    try:
                        child.activated.disconnect()
                    except:  # noqa: E722
                        pass
                    child.setParent(None)
                    child.deleteLater()
            except Exception:
                continue

        shortcut = qt.QShortcut(self.parentWindow)
        shortcut.setKey(targetSequence)
        shortcut.setContext(qt.Qt.ApplicationShortcut)
        shortcut.activated.connect(callback)
        shortcut.setEnabled(True)

        self.shortcuts[keySequence] = shortcut

    def setupShortcuts(self):
        self.addShortcut("d", self.AddAblPoint)
        self.addShortcut("m", self.AddMapPoint)
        self.addShortcut("e", self.EditActivePoint)
        self.addShortcut("Delete", self.DeleteActivePoint)

    # ----------------------------------------------------------------------
    # DELETE ACTIVE POINT
    # ----------------------------------------------------------------------
    def DeleteActivePoint(self):
        self.syncParameterNode()

        node = getattr(self.pNode, "ablationPts", None)
        dn = node.GetDisplayNode() if node else None
        activeIndex = dn.GetActiveControlPoint() if dn else -1

        if activeIndex < 0:
            node = getattr(self.pNode, "mappingPts", None)
            dn = node.GetDisplayNode() if node else None
            activeIndex = dn.GetActiveControlPoint() if dn else -1

        if not node or activeIndex < 0:
            return

        dn.SetActiveControlPoint(-1)
        node.RemoveNthControlPoint(activeIndex)

        slicer.util.mainWindow().setFocus(qt.Qt.ActiveWindowFocusReason)

    # ----------------------------------------------------------------------
    # EDIT ACTIVE POINT
    # ----------------------------------------------------------------------
    def EditActivePoint(self):
        self.syncParameterNode()

        node = getattr(self.pNode, "ablationPts", None)
        dn = node.GetDisplayNode() if node else None
        activeIndex = dn.GetActiveControlPoint() if dn else -1

        if activeIndex < 0:
            node = getattr(self.pNode, "mappingPts", None)
            dn = node.GetDisplayNode() if node else None
            activeIndex = dn.GetActiveControlPoint() if dn else -1

        if not node or node.GetNumberOfControlPoints() == 0:
            logging.info("ShortcutManager: No markup node available for editing.")
            return

        if activeIndex < 0:
            activeIndex = node.GetNumberOfControlPoints() - 1
        if activeIndex < 0:
            logging.info("ShortcutManager: No control point available for editing.")
            return

        slicer.util.selectModule("Markups")
        slicer.app.processEvents()

        slicer.modules.markups.logic().SetActiveListID(node)
        slicer.app.processEvents()

        mw = slicer.modules.markups.widgetRepresentation()
        cpSection = mw.findChild(ctk.ctkCollapsibleButton, "controlPointsCollapsibleButton")
        if cpSection:
            cpSection.setChecked(True)
        slicer.app.processEvents()

        table = mw.findChild(qt.QTableWidget, "activeMarkupTableWidget")
        if table and 0 <= activeIndex < table.rowCount:
            table.setCurrentCell(activeIndex, 0)
            item = table.item(activeIndex, 0)
            if item:
                table.scrollToItem(item, qt.QAbstractItemView.PositionAtCenter)
            table.repaint()

        slicer.app.processEvents()

        logging.info(f"ShortcutManager: Editing point {activeIndex} in node {node.GetName()}")

    # ----------------------------------------------------------------------
    # SURFACE SNAP (Ruff-clean)
    # ----------------------------------------------------------------------
    def getSnappedPosition(self, inputPos, maxDistance=1.0):
        """
        Calculates the nearest point on the raModel surface.
        NOTE: 'Effectively DISABLED' with maxDistance = 1.0  -> snapping 'OFF'
        Uses vtk.reference() but typed as Any to satisfy Ruff.
        """
        raModel = getattr(self.pNode, "raModel", None)
        if not raModel:
            return inputPos

        polyData = raModel.GetPolyData()
        if not polyData or polyData.GetNumberOfPoints() == 0:
            return inputPos

        if raModel.GetParentTransformNode():
            transformedPoly = vtk.vtkPolyData()
            slicer.modules.models.logic().GetPolyDataWithAppliedTransform(raModel, transformedPoly)
            polyData = transformedPoly

        cellLocator = vtk.vtkCellLocator()
        cellLocator.SetDataSet(polyData)
        cellLocator.BuildLocator()

        closestPoint = [0.0, 0.0, 0.0]
        cellId: Any = vtk.reference(0)
        subId: Any = vtk.reference(0)
        dist2: Any = vtk.reference(0.0)

        cellLocator.FindClosestPoint(inputPos, closestPoint, cellId, subId, dist2)

        import math

        distance = math.sqrt(float(dist2))

        if distance <= maxDistance:
            return [closestPoint[0], closestPoint[1], closestPoint[2]]

        return inputPos

    # ----------------------------------------------------------------------
    # STYLING
    # ----------------------------------------------------------------------
    def applyAblationPointStyle(self, node):
        if not node:
            return
        node.CreateDefaultDisplayNodes()
        dn = node.GetDisplayNode()
        if not dn or not dn.IsA("vtkMRMLMarkupsDisplayNode"):
            return

        dn.SetGlyphTypeFromString("Sphere3D")
        dn.SetGlyphSize(4.0)
        dn.SetUseGlyphScale(False)

        dn.SetSelectedColor([1.0, 0.5, 0.5])
        dn.SetActiveColor([0.75, 0.10, 0.10])

        dn.SetPointLabelsVisibility(False)
        dn.SetVisibility(1)
        dn.SetOpacity(1.0)
        dn.SetPropertiesLabelVisibility(False)
        dn.SetOccludedVisibility(True)

        dn.SetSpecular(1.0)
        dn.SetPower(80)
        dn.Modified()

    def applyMappingPointStyle(self, node):
        if not node:
            return
        node.CreateDefaultDisplayNodes()
        dn = node.GetDisplayNode()
        if not dn or not dn.IsA("vtkMRMLMarkupsDisplayNode"):
            return

        dn.SetGlyphSize(4.0)
        dn.SetUseGlyphScale(False)
        dn.SetSnapMode(1)

        dn.SetSelectedColor([0.0, 0.4, 0.1])
        dn.SetActiveColor([0.0, 0.75, 0.25])

        dn.SetPointLabelsVisibility(True)
        dn.SetTextScale(2.6)
        dn.SetVisibility(1)
        dn.SetOpacity(1.0)
        dn.SetOccludedVisibility(True)
        dn.Modified()

    # ----------------------------------------------------------------------
    # ABLATION POINT (manual)
    # ----------------------------------------------------------------------
    def AddAblPoint(self):
        self.syncParameterNode()
        isReplaying = getattr(self.pNode, "replayModeActive", False)
        if callable(isReplaying):
            isReplaying = isReplaying()

        if str(isReplaying).lower() == "true" or isReplaying == 1:
            return

        self.transformNode_abl = getattr(self.pNode, "ablTransform", None)
        if not self.transformNode_abl:
            self.transformNode_abl = slicer.mrmlScene.GetFirstNodeByName("Abl_01_TF")
        if not self.transformNode_abl:
            return

        try:
            tipPos_attr = self.transformNode_abl.GetAttribute("OpenIGTLink.tipPos")
            if not tipPos_attr:
                return
            tipPos = [float(item) for item in tipPos_attr.strip("[]").replace(" ", "").split(",")]
        except:  # noqa: E722
            return

        finalPos = self.getSnappedPosition(tipPos)
        node = getattr(self.pNode, "ablationPts", None)

        if not node:
            node = slicer.mrmlScene.GetFirstNodeByName("ablationPts") or slicer.mrmlScene.AddNewNodeByClass(
                "vtkMRMLMarkupsFiducialNode", "ablationPts"
            )
            if self.pNode:
                self.pNode.ablationPts = node

        self.applyAblationPointStyle(node)

        wasModifying = node.StartModify()
        node.AddControlPoint(vtk.vtkVector3d(finalPos[0], finalPos[1], finalPos[2]), "")
        node.EndModify(wasModifying)

        slicer.util.mainWindow().setFocus(qt.Qt.ActiveWindowFocusReason)

    # ----------------------------------------------------------------------
    # MAPPING POINT (STRING-triggered, using cached tip)
    # ----------------------------------------------------------------------
    def AddMapPointWithCachedTip(self, label, tipPos):
        logging.info(f"[SM] AddMapPointWithCachedTip(label={label}, tipPos={tipPos})")

        """
        Places a mapping point using a cached tip position provided by EPCMRLogic.
        This is the correct, race-free method for IGTL STRING-triggered placement.
        """
        self.syncParameterNode()

        node = getattr(self.pNode, "mappingPts", None)
        if not node:
            node = slicer.mrmlScene.GetFirstNodeByName("mappingPts") or slicer.mrmlScene.AddNewNodeByClass(
                "vtkMRMLMarkupsFiducialNode", "mappingPts"
            )
            if self.pNode:
                self.pNode.mappingPts = node

        self.applyMappingPointStyle(node)

        finalPos = self.getSnappedPosition(tipPos)

        wasModifying = node.StartModify()
        idx = node.AddControlPoint(vtk.vtkVector3d(*finalPos), str(label))
        node.SetNthControlPointLocked(idx, False)
        node.EndModify(wasModifying)

        logging.info(f"ShortcutManager: Mapping point added at {finalPos} with label '{label}'.")
        slicer.util.mainWindow().setFocus(qt.Qt.ActiveWindowFocusReason)
        logging.info(f"[SM] mappingPts now has {node.GetNumberOfControlPoints()} points")

    # ----------------------------------------------------------------------
    # MAPPING POINT (manual "m" key)
    # ----------------------------------------------------------------------
    def AddMapPoint(self):
        """
        Manual mapping point placement (keyboard 'm').
        Uses live tipPos from transform node.
        """
        self.syncParameterNode()
        isReplaying = getattr(self.pNode, "replayModeActive", False)
        if callable(isReplaying):
            isReplaying = isReplaying()
        if str(isReplaying).lower() == "true" or isReplaying == 1:
            return

        self.transformNode_abl = getattr(self.pNode, "ablTransform", None)
        if not self.transformNode_abl:
            self.transformNode_abl = slicer.mrmlScene.GetFirstNodeByName("Abl_01_TF")
        if not self.transformNode_abl:
            return

        try:
            tipPos_attr = self.transformNode_abl.GetAttribute("OpenIGTLink.tipPos")
            if not tipPos_attr:
                return
            tipPos = [float(item) for item in tipPos_attr.strip("[]").replace(" ", "").split(",")]
        except:  # noqa: E722
            return

        finalPos = self.getSnappedPosition(tipPos)
        node = getattr(self.pNode, "mappingPts", None)

        if not node:
            node = slicer.mrmlScene.GetFirstNodeByName("mappingPts") or slicer.mrmlScene.AddNewNodeByClass(
                "vtkMRMLMarkupsFiducialNode", "mappingPts"
            )
            if self.pNode:
                self.pNode.mappingPts = node

        self.applyMappingPointStyle(node)

        at_value = qt.QInputDialog.getText(slicer.util.mainWindow(), "Activation Time / Voltage", "Measurement:")

        if at_value:
            raw_value = at_value.strip()

            cleaned = raw_value.replace(",", ".")
            import re

            match = re.search(r"[-+]?\d*\.?\d+", cleaned)
            if match:
                cleaned = match.group(0)
            else:
                cleaned = "0.0"

            try:
                numeric_value = float(cleaned)
            except Exception:
                numeric_value = 0.0

            mode = getattr(self.pNode, "mappingMode", "")

            if mode == "Activation Time Mapping":
                label_str = f"{int(round(numeric_value))}"
            else:
                label_str = f"{numeric_value:.2f}"

            wasModifying = node.StartModify()
            idx = node.AddControlPoint(vtk.vtkVector3d(finalPos[0], finalPos[1], finalPos[2]), label_str)
            node.SetNthControlPointLocked(idx, False)
            node.EndModify(wasModifying)

        slicer.util.mainWindow().setFocus(qt.Qt.ActiveWindowFocusReason)

    # ----------------------------------------------------------------------
    # CLEANUP
    # ----------------------------------------------------------------------
    def cleanup(self):
        """Safely disconnects and deletes shortcuts."""
        if not hasattr(self, "shortcuts") or not self.shortcuts:
            return

        for key in list(self.shortcuts.keys()):
            shortcut = self.shortcuts.pop(key)
            if not shortcut:
                continue
            try:
                shortcut.setEnabled(False)
                shortcut.activated.disconnect()
                shortcut.setParent(None)
                shortcut.deleteLater()
            except:  # noqa: E722
                pass

        self.shortcuts = {}
        logging.info("ShortcutManager: Cleaned up.")

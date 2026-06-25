# FreeAngulatorLogic.py

import slicer
import vtk
from slicer.ScriptedLoadableModule import ScriptedLoadableModuleLogic
from EPCMRLib.EPCMRParameterNode import EPCMRParameterNode


class FreeAngulatorLogic(ScriptedLoadableModuleLogic):
    """
    Logic class for Free Angulator geometry storage, restoration, and deletion.

    Responsibilities:
      - Store 4x4 SliceToRAS matrices for Red/Green/Yellow slice views
      - Restore those matrices
      - Delete stored geometries
      - List stored geometries

    Integration with EPCMRParameterNode:
      - Mirrors geometry into:
            freeAngulatorStoredNames
            freeAngulatorGeometry
      - Allows EPCMR workflow components to access geometry state

    Slicer 5.7 compatibility:
      - No SetSliceToRAS(), so we DeepCopy into GetSliceToRAS() and call UpdateMatrices().
    """

    def __init__(self):
        super().__init__()
        self.paramNode = self.getOrCreateParamNode()

    # --------------------------------------------------------------------------
    # EPCMRParameterNode integration
    # --------------------------------------------------------------------------

    def getEPCMRParameterNodeWrapper(self):
        """
        Retrieve EPCMRParameterNode wrapper if its underlying MRML node exists.
        """
        try:
            node = slicer.mrmlScene.GetFirstNodeByName("EPCMRParameterNode")
            if node:
                return EPCMRParameterNode(node)
        except Exception:
            pass
        return None

    # --------------------------------------------------------------------------
    # Local FreeAngulator parameter node
    # --------------------------------------------------------------------------

    def getOrCreateParamNode(self):
        """
        Create or retrieve the ScriptedModuleNode used to store slice geometries.
        """
        node = slicer.mrmlScene.GetFirstNodeByName("EPCMR_FreeAngulator")
        if node is None:
            node = slicer.vtkMRMLScriptedModuleNode()
            node.SetName("EPCMR_FreeAngulator")
            slicer.mrmlScene.AddNode(node)
        return node

    # --------------------------------------------------------------------------
    # Matrix serialization helpers
    # --------------------------------------------------------------------------

    def matrixToString(self, m):
        vals = []
        for r in range(4):
            for c in range(4):
                vals.append(str(m.GetElement(r, c)))
        return " ".join(vals)

    def stringToMatrix(self, s):
        parts = s.strip().split()
        if len(parts) != 16:
            raise ValueError("Expected 16 values for matrix")
        m = vtk.vtkMatrix4x4()
        idx = 0
        for r in range(4):
            for c in range(4):
                m.SetElement(r, c, float(parts[idx]))
                idx += 1
        return m

    # --------------------------------------------------------------------------
    # Store geometry
    # --------------------------------------------------------------------------

    def storeTargetGeometry(self, name):
        """
        Store the current Red/Green/Yellow slice geometries under a single logical name.
        Mirrors into EPCMRParameterNode.
        """
        lm = slicer.app.layoutManager()
        if not lm:
            print("FreeAngulatorLogic.storeTargetGeometry: No layout manager.")
            return

        epcmrNode = self.getEPCMRParameterNodeWrapper()

        def storeOne(sliceName):
            sw = lm.sliceWidget(sliceName)
            if not sw:
                print(f"FreeAngulatorLogic.storeTargetGeometry: Slice {sliceName} not found.")
                return

            sliceNode = sw.sliceLogic().GetSliceNode()
            m = sliceNode.GetSliceToRAS()
            serialized = self.matrixToString(m)

            key = f"{name}_{sliceName}"

            # Local node
            self.paramNode.SetAttribute(key, serialized)

            # EPCMR mirror
            if epcmrNode is not None:
                geom = epcmrNode.freeAngulatorGeometry
                geom[key] = serialized
                epcmrNode.freeAngulatorGeometry = geom

        for s in ("Red", "Green", "Yellow"):
            storeOne(s)

        # Maintain list of geometry names (local)
        namesAttr = self.paramNode.GetAttribute("Names")
        names = [n for n in namesAttr.split(",") if n.strip()] if namesAttr else []

        if name not in names:
            names.append(name)
            self.paramNode.SetAttribute("Names", ",".join(names))

        # Mirror list into EPCMRParameterNode
        if epcmrNode is not None:
            epcmrNode.freeAngulatorStoredNames = names

        print(f"FreeAngulatorLogic: Stored target geometry '{name}'.")

    # --------------------------------------------------------------------------
    # Restore geometry
    # --------------------------------------------------------------------------

    def restoreTargetGeometry(self, name):
        """
        Restore Red/Green/Yellow slice geometries from a stored target geometry name.
        """
        lm = slicer.app.layoutManager()
        if not lm:
            print("FreeAngulatorLogic.restoreTargetGeometry: No layout manager.")
            return

        epcmrNode = self.getEPCMRParameterNodeWrapper()

        # Collect slice info
        sliceInfo = {}
        for sliceName in ("Red", "Green", "Yellow"):
            sw = lm.sliceWidget(sliceName)
            if not sw:
                print(f"FreeAngulatorLogic.restoreTargetGeometry: Slice {sliceName} not found.")
                continue
            logic = sw.sliceLogic()
            sliceNode = logic.GetSliceNode()
            compNode = logic.GetSliceCompositeNode()
            sliceInfo[sliceName] = (sliceNode, compNode)

        if not sliceInfo:
            print("FreeAngulatorLogic.restoreTargetGeometry: No slice widgets available.")
            return

        # Temporarily disable linking
        prevLinked = {}
        for sliceName, (sliceNode, compNode) in sliceInfo.items():
            prevLinked[sliceName] = compNode.GetLinkedControl()
            compNode.SetLinkedControl(False)

        # Apply stored matrices
        def restoreOne(sliceName):
            key = f"{name}_{sliceName}"

            # Prefer EPCMRParameterNode
            serialized = None
            if epcmrNode is not None:
                serialized = epcmrNode.freeAngulatorGeometry.get(key)

            # Fallback to local node
            if not serialized:
                serialized = self.paramNode.GetAttribute(key)

            if not serialized:
                print(f"FreeAngulatorLogic.restoreTargetGeometry: No stored geometry for '{key}'.")
                return

            storedM = self.stringToMatrix(serialized)
            sliceNode, compNode = sliceInfo[sliceName]

            currentM = sliceNode.GetSliceToRAS()
            currentM.DeepCopy(storedM)

            sliceNode.UpdateMatrices()
            sliceNode.Modified()

        for s in ("Red", "Green", "Yellow"):
            restoreOne(s)

        # Restore linking
        for sliceName, (sliceNode, compNode) in sliceInfo.items():
            compNode.SetLinkedControl(prevLinked[sliceName])

        slicer.util.forceRenderAllViews()
        print(f"FreeAngulatorLogic: Restored target geometry '{name}'.")

    # --------------------------------------------------------------------------
    # Delete geometry
    # --------------------------------------------------------------------------

    def deleteTargetGeometry(self, name):
        """
        Delete a stored target geometry from both:
          - Local FreeAngulator node
          - EPCMRParameterNode
        """
        epcmrNode = self.getEPCMRParameterNodeWrapper()

        # Remove attributes from local node
        for sliceName in ("Red", "Green", "Yellow"):
            key = f"{name}_{sliceName}"
            self.paramNode.RemoveAttribute(key)

            if epcmrNode is not None:
                geom = epcmrNode.freeAngulatorGeometry
                if key in geom:
                    del geom[key]
                epcmrNode.freeAngulatorGeometry = geom

        # Update local Names list
        namesAttr = self.paramNode.GetAttribute("Names")
        names = [n for n in namesAttr.split(",") if n.strip()] if namesAttr else []

        if name in names:
            names.remove(name)
            self.paramNode.SetAttribute("Names", ",".join(names))

        # Mirror into EPCMRParameterNode
        if epcmrNode is not None:
            epcmrNode.freeAngulatorStoredNames = names

        print(f"FreeAngulatorLogic: Deleted target geometry '{name}'.")

    # --------------------------------------------------------------------------
    # List geometries
    # --------------------------------------------------------------------------

    def listGeometries(self):
        """
        Return a list of all stored geometry names.
        Prefer EPCMRParameterNode if available.
        """
        epcmrNode = self.getEPCMRParameterNodeWrapper()
        if epcmrNode is not None:
            names = epcmrNode.freeAngulatorStoredNames
            if names:
                return names

        namesAttr = self.paramNode.GetAttribute("Names")
        return [n for n in namesAttr.split(",") if n.strip()] if namesAttr else []

# EPCMRLib/Utilities/RendererRepairManager.py
"""
RendererRepairManager

Detects and repairs common Slicer/VTK renderer lighting and GL state corruption
that causes scenes to remain dark after module reloads, sandbox reloads,
layout changes, or GPU context events.

Usage:
    from EPCMRLib.Utilities.RendererRepairManager import RendererRepairManager
    rrm = RendererRepairManager()
    rrm.repairAllRenderers()

The repair is non-destructive: it attempts safe toggles (multisampling, FXAA,
depth peeling, automatic headlight) and re-installs lights via a provided
lightsManager callback if available.
"""

import logging
import time

import slicer
import vtk

class RendererRepairManager:
    def __init__(self, lightsManagerCallback=None, logger=None):
        """
        lightsManagerCallback: optional callable that will be invoked to
            re-install lights after renderer reset. Signature: callable()
            If None, the manager will attempt to call:
            slicer.modules.epcmr.widgetRepresentation().self().logic.sceneManager.lightsManager
            if available.
        logger: optional logger-like object with debug/info/warning methods.
        """
        self.lightsManagerCallback = lightsManagerCallback
        self.logger = logger or logging.getLogger("RendererRepairManager")

    # -------------------------
    # Public API
    # -------------------------
    def repairAllRenderers(self):
        lm = slicer.app.layoutManager()
        if not lm:
            self.logger.warning("RendererRepairManager: no layoutManager available")
            return
        for i in range(lm.threeDViewCount):
            try:
                threeDWidget = lm.threeDWidget(i)
                if not threeDWidget:
                    continue
                view = threeDWidget.threeDView()
                if not view:
                    continue
                self.repairRenderer(view)
            except Exception as e:
                self.logger.warning(f"RendererRepairManager: repair for view {i} failed: {e}")

    def repairRenderer(self, view):
        """
        Attempt to repair a single 3D view renderer.
        """
        try:
            rw = view.renderWindow()
            ren = rw.GetRenderers().GetFirstRenderer()
            if not ren:
                self.logger.debug("RendererRepairManager: no renderer found")
                return

            self.logger.info("RendererRepairManager: starting repair for renderer")

            # 1) Ensure multisampling is enabled (helps depth peeling and correct shading)
            try:
                # Try a conservative multisample value
                rw.SetMultiSamples(8)
                self.logger.debug("Set renderWindow multisamples to 8")
            except Exception:
                self.logger.debug("SetMultiSamples not supported or failed")

            # 2) Turn off FXAA (FXAA can disable multisampling combos)
            try:
                # Use attribute if available
                if hasattr(ren, "UseFXAAOff"):
                    ren.UseFXAAOff()
                    self.logger.debug("Called UseFXAAOff()")
                else:
                    # Some VTK builds expose UseFXAA as SetUseFXAA
                    try:
                        ren.SetUseFXAA(0)
                        self.logger.debug("Called SetUseFXAA(0)")
                    except Exception:
                        self.logger.debug("FXAA toggle not available")
            except Exception:
                self.logger.debug("FXAA toggle failed")

            # 3) Temporarily disable depth peeling to avoid broken pipeline
            try:
                if hasattr(ren, "SetUseDepthPeeling"):
                    ren.SetUseDepthPeeling(0)
                    self.logger.debug("Disabled depth peeling temporarily")
            except Exception:
                self.logger.debug("Disabling depth peeling failed")

            # 4) Disable automatic headlight so scene lights are used
            try:
                if hasattr(ren, "SetAutomaticLightCreation"):
                    ren.SetAutomaticLightCreation(0)
                    self.logger.debug("Disabled automatic headlight (SetAutomaticLightCreation(0))")
                else:
                    # fallback: try to remove any headlight explicitly later
                    self.logger.debug("SetAutomaticLightCreation not available")
            except Exception:
                self.logger.debug("SetAutomaticLightCreation failed")

            # 5) Remove all existing lights (safe sweep)
            try:
                lights = ren.GetLights()
                lights.InitTraversal()
                l = lights.GetNextItem()
                removed = 0
                while l:
                    try:
                        ren.RemoveLight(l)
                        removed += 1
                    except Exception:
                        pass
                    l = lights.GetNextItem()
                self.logger.debug(f"Removed {removed} existing lights")
            except Exception:
                self.logger.debug("Failed to sweep lights")

            # 6) Force a GL reinit by rendering and a short pause
            try:
                rw.Render()
                # small pause to allow GL driver to settle
                time.sleep(0.05)
                self.logger.debug("Forced render to reinit GL state")
            except Exception:
                self.logger.debug("Render call failed during repair")

            # 7) Reinstall lights via callback or via EPCMR lightsManager if available
            try:
                if self.lightsManagerCallback:
                    self.logger.debug("Calling provided lightsManagerCallback()")
                    self.lightsManagerCallback()
                else:
                    # Try to find EPCMR lights manager automatically
                    try:
                        w = slicer.modules.epcmr.widgetRepresentation().self()
                        logic = getattr(w, "logic", None)
                        if logic and hasattr(logic, "sceneManager") and hasattr(logic.sceneManager, "lightsManager"):
                            lmgr = logic.sceneManager.lightsManager
                            self.logger.debug("Found EPCMR lightsManager; calling resetLighting/setupLighting")
                            try:
                                lmgr.resetLighting()
                                lmgr.setupLighting()
                            except Exception:
                                # fallback: call setupLighting only
                                try:
                                    lmgr.setupLighting()
                                except Exception:
                                    self.logger.debug("EPCMR lightsManager calls failed")
                        else:
                            self.logger.debug("No EPCMR lightsManager found")
                    except Exception:
                        self.logger.debug("Automatic lightsManager discovery failed")
            except Exception as e:
                self.logger.debug(f"lightsManager reinstall failed: {e}")

            # 8) Re-enable depth peeling only if multisampling is available and supported
            try:
                # Try enabling depth peeling if supported
                if hasattr(ren, "SetUseDepthPeeling"):
                    ren.SetUseDepthPeeling(1)
                    ren.SetMaximumNumberOfPeels(50)
                    ren.SetOcclusionRatio(0.1)
                    self.logger.debug("Re-enabled depth peeling")
            except Exception:
                self.logger.debug("Re-enabling depth peeling failed; leaving it disabled")

            # 9) Final render and sanity check
            try:
                rw.Render()
                slicer.util.forceRenderAllViews()
                self.logger.info("RendererRepairManager: repair sequence completed; run sphere test to verify")
            except Exception:
                self.logger.warning("RendererRepairManager: final render failed")

            # 10) Quick verification: count lights
            try:
                count = ren.GetLights().GetNumberOfItems()
                self.logger.info(f"RendererRepairManager: lights after repair: {count}")
            except Exception:
                pass

        except Exception as e:
            self.logger.warning(f"RendererRepairManager: unexpected error during repair: {e}")

    # -------------------------
    # Convenience helpers
    # -------------------------
    @staticmethod
    def quickSphereTest(viewIndex=0):
        """
        Add a temporary sphere actor to the specified 3D view to visually verify shading.
        Returns the actor so caller can remove it later.
        """
        try:
            view = slicer.app.layoutManager().threeDWidget(viewIndex).threeDView()
            ren = view.renderWindow().GetRenderers().GetFirstRenderer()
            sphere = vtk.vtkSphereSource()
            sphere.SetRadius(20)
            sphere.SetThetaResolution(48)
            sphere.SetPhiResolution(48)
            sphere.Update()
            mapper = vtk.vtkPolyDataMapper()
            mapper.SetInputConnection(sphere.GetOutputPort())
            actor = vtk.vtkActor()
            actor.SetMapper(mapper)
            actor.GetProperty().SetColor(1.0, 0.6, 0.2)
            actor.GetProperty().SetDiffuse(0.95)
            actor.GetProperty().SetSpecular(0.05)
            actor.SetPosition(0,0,0)
            ren.AddActor(actor)
            view.forceRender()
            return actor
        except Exception:
            return None

# End of RendererRepairManager.py

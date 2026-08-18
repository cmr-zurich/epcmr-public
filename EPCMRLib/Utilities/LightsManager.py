# EPCMRLib/Utilities/LightsManager.py

import logging

import slicer
import vtk


class LightsManager:
    """
    EPCMR LightsManager

    Responsible for:
      - Installing CARTO-style balanced lighting into all 3D views
      - Tracking lights per view for deterministic teardown
      - Resetting/reinstalling lights without double-stacking
      - Configuring renderer quality (FXAA, depth peeling)
    """

    def __init__(self) -> None:
        # Track lights per view so we can remove them deterministically
        self._lightsPerView = {}
        self._lightingInstalled = False

    # -------------------------------------------------------------------------
    # Public API
    # -------------------------------------------------------------------------

    def setupLighting(self) -> None:
        """
        Balanced CARTO-style lighting with catheter safety.

        Idempotent across resets:
          - Lights are installed only once per LightsManager lifetime.
          - Lights are tracked per view and removed in resetLighting()/cleanup().
        """
        if getattr(self, "_lightingInstalled", False):
            return

        lm = slicer.app.layoutManager()
        if not lm:
            logging.warning("LightsManager.setupLighting: no layoutManager available")
            return

        # Track lights per view so we can remove them deterministically in reset/cleanup
        if not hasattr(self, "_lightsPerView"):
            self._lightsPerView = {}

        for i in range(lm.threeDViewCount):
            threeDWidget = lm.threeDWidget(i)
            if not threeDWidget:
                continue
            view = threeDWidget.threeDView()
            if not view:
                continue
            viewNode = view.mrmlViewNode()
            if not viewNode:
                continue
            vid = viewNode.GetID()
            renderer = view.renderWindow().GetRenderers().GetFirstRenderer()
            if not renderer:
                continue

            # Remove all existing lights to prevent double-stacking on cold startup
            lights = renderer.GetLights()
            lights.InitTraversal()
            existingLights = []
            l = lights.GetNextItem()
            while l:
                existingLights.append(l)
                l = lights.GetNextItem()
            for l in existingLights:
                try:
                    renderer.RemoveLight(l)
                except Exception:
                    pass

            # Remove default headlight
            lights = renderer.GetLights()
            lights.InitTraversal()
            head = lights.GetNextItem()
            if head:
                try:
                    renderer.RemoveLight(head)
                except Exception:
                    pass

            viewLights = []

            # Rim light
            rim = vtk.vtkLight()
            rim.SetLightTypeToSceneLight()
            rim.SetPosition(-140, -310, 210)
            rim.SetFocalPoint(0, 0, 0)
            rim.SetColor(0.60, 0.70, 1.00)
            rim.SetIntensity(0.28)
            renderer.AddLight(rim)
            viewLights.append(rim)

            # Fill light
            fill = vtk.vtkLight()
            fill.SetLightTypeToSceneLight()
            fill.SetPosition(0, 300, 120)
            fill.SetFocalPoint(0, 0, 0)
            fill.SetColor(1.00, 0.85, 0.70)
            fill.SetIntensity(0.22)
            renderer.AddLight(fill)
            viewLights.append(fill)

            # Catheter lights
            catFront = vtk.vtkLight()
            catFront.SetLightTypeToSceneLight()
            catFront.SetPosition(120, -80, 180)
            catFront.SetFocalPoint(0, 0, 0)
            catFront.SetColor(1.00, 1.00, 1.00)
            catFront.SetIntensity(0.85)
            renderer.AddLight(catFront)
            viewLights.append(catFront)

            catRear = vtk.vtkLight()
            catRear.SetLightTypeToSceneLight()
            catRear.SetPosition(-120, 80, -160)
            catRear.SetFocalPoint(0, 0, 0)
            catRear.SetColor(1.00, 1.00, 1.00)
            catRear.SetIntensity(0.65)
            renderer.AddLight(catRear)
            viewLights.append(catRear)

            catTop = vtk.vtkLight()
            catTop.SetLightTypeToSceneLight()
            catTop.SetPosition(0, 0, 260)
            catTop.SetFocalPoint(0, 0, 0)
            catTop.SetColor(1.00, 1.00, 1.00)
            catTop.SetIntensity(0.55)
            renderer.AddLight(catTop)
            viewLights.append(catTop)

            for catLight in (catFront, catRear, catTop):
                catLight.SetAmbientColor(1.0, 1.0, 1.0)
                catLight.SetDiffuseColor(1.0, 1.0, 1.0)
                catLight.SetSpecularColor(1.0, 1.0, 1.0)

            # Ambient anatomy light
            ambient = vtk.vtkLight()
            ambient.SetLightTypeToSceneLight()
            ambient.SetPosition(0, 0, 0)
            ambient.SetFocalPoint(0, 0, 0)
            ambient.SetColor(0.95, 0.95, 1.00)
            ambient.SetIntensity(0.75)
            renderer.AddLight(ambient)
            viewLights.append(ambient)

            # Under-light
            under = vtk.vtkLight()
            under.SetLightTypeToSceneLight()
            under.SetPosition(0, -400, -200)
            under.SetFocalPoint(0, 0, 0)
            under.SetColor(0.85, 0.90, 1.00)
            under.SetIntensity(0.75)
            renderer.AddLight(under)
            viewLights.append(under)

            # No-shadow ambient light
            noShadow = vtk.vtkLight()
            noShadow.SetLightTypeToSceneLight()
            noShadow.SetPosition(0, 0, 0)
            noShadow.SetFocalPoint(0, 0, 0)
            noShadow.SetColor(1.0, 1.0, 1.0)
            noShadow.SetIntensity(0.55)
            noShadow.SetAmbientColor(1.0, 1.0, 1.0)
            noShadow.SetDiffuseColor(0.0, 0.0, 0.0)
            noShadow.SetSpecularColor(0.0, 0.0, 0.0)
            renderer.AddLight(noShadow)
            viewLights.append(noShadow)

            self._lightsPerView[vid] = viewLights

            # Renderer quality settings
            try:
                renderer.UseFXAAOn()
                renderer.SetUseDepthPeeling(1)
                renderer.SetMaximumNumberOfPeels(50)
                renderer.SetOcclusionRatio(0.1)
            except Exception:
                logging.warning("LightsManager.setupLighting: renderer quality settings failed")

        # Unified viewport synchronization pass to avoid mid-loop pipeline stalls
        for i in range(lm.threeDViewCount):
            threeDWidget = lm.threeDWidget(i)
            if threeDWidget:
                view = threeDWidget.threeDView()
                if view:
                    try:
                        view.forceRender()
                    except Exception:
                        pass

        self._lightingInstalled = True

    def resetLighting(self) -> None:
        """
        Tears down tracked EPCMR lights and forces setupLighting to run cleanly again.
        """
        # Flip the safety guard flag to bypass the early-return block
        self._lightingInstalled = False

        lm = slicer.app.layoutManager()
        if not lm or not hasattr(self, "_lightsPerView"):
            # If no lights are tracked yet, just run standard setup safely
            self.setupLighting()
            return

        # Explicitly sweep away tracked lights from active view render pipelines
        for i in range(lm.threeDViewCount):
            threeDWidget = lm.threeDWidget(i)
            if not threeDWidget:
                continue
            view = threeDWidget.threeDView()
            if not view:
                continue
            viewNode = view.mrmlViewNode()
            if not viewNode:
                continue
            vid = viewNode.GetID()

            renderer = view.renderWindow().GetRenderers().GetFirstRenderer()
            if not renderer:
                continue

            # Remove only the custom lights we registered for this view ID
            trackedLights = self._lightsPerView.get(vid, [])
            for light in trackedLights:
                try:
                    renderer.RemoveLight(light)
                except Exception:
                    pass

            # Clear the tracking list for this viewport
            self._lightsPerView[vid] = []

        # Re-execute the complete setup pass clean
        self.setupLighting()

    def cleanup(self) -> None:
        """
        Remove all tracked lights without reinstalling.

        Call this from SceneManager.cleanup() or module shutdown.
        """
        lm = slicer.app.layoutManager()
        if not lm or not hasattr(self, "_lightsPerView"):
            return

        for i in range(lm.threeDViewCount):
            threeDWidget = lm.threeDWidget(i)
            if not threeDWidget:
                continue
            view = threeDWidget.threeDView()
            if not view:
                continue
            viewNode = view.mrmlViewNode()
            if not viewNode:
                continue
            vid = viewNode.GetID()

            renderer = view.renderWindow().GetRenderers().GetFirstRenderer()
            if not renderer:
                continue

            trackedLights = self._lightsPerView.get(vid, [])
            for light in trackedLights:
                try:
                    renderer.RemoveLight(light)
                except Exception:
                    pass

            self._lightsPerView[vid] = []

        self._lightingInstalled = False

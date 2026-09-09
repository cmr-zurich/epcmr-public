# EPCMRLib/Utilities/LightsManager.py

import logging

import slicer
import vtk

# ---------------------------------------------------------------------------
# EPCMR Architecture: LightsManager
#
# Role:
#   - Owns all lighting rigs used by EPCMR.
#   - Provides deterministic setup and teardown of lights in all 3D views.
#   - Ensures consistent illumination for anatomy, catheters, overlays,
#     and scalar bars.
#   - Prevents accumulation of lights across reloads or scene resets.
#
# Relationship to SceneManager:
#   - SceneManager orchestrates when lighting should be applied or reset.
#   - LightsManager performs the actual VTK lighting operations.
#   - SceneManager never manipulates raw VTK light objects directly.
#
# Relationship to MaterialManager:
#   - MaterialManager handles shading and material properties.
#   - LightsManager handles illumination and light sources.
#   - Both managers are independent but complementary.
#
# Result:
#   - Clear separation of concerns:
#       SceneManager = coordinator
#       MaterialManager = materials and rim glow
#       LightsManager = lighting rig
#   - Predictable rendering behavior and easier maintenance.
# ---------------------------------------------------------------------------


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

        # ------------------------------------------------------------------
        # Ensure renderer GL/lighting state is healthy before installing lights
        # ------------------------------------------------------------------
        # Run a non-destructive renderer repair once per LightsManager instance.
        # This prevents adding lights into a corrupted GL state (common after
        # sandbox/module reloads). The guard keeps this idempotent per instance.
        try:
            if not getattr(self, "_rendererRepairAttempted", False):
                from PyQt5 import QtCore

                def _lightsmanager_repair():
                    try:
                        from EPCMRLib.Utilities.RendererRepairManager import RendererRepairManager

                        try:
                            RendererRepairManager().repairAllRenderers()
                        except Exception:
                            pass
                    except Exception:
                        pass
                    self._rendererRepairAttempted = True

                # If layoutManager exists now, run immediately; otherwise defer slightly.
                try:
                    if slicer.app.layoutManager():
                        _lightsmanager_repair()
                    else:
                        QtCore.QTimer.singleShot(250, _lightsmanager_repair)
                except Exception:
                    # Fallback: attempt immediate repair but never raise
                    try:
                        _lightsmanager_repair()
                    except Exception:
                        self._rendererRepairAttempted = True
        except Exception:
            # Defensive: any attribute errors should not break setupLighting
            pass

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

            # ------------------------------------------------------------------
            # CRITICAL: ensure renderer uses scene lights and not automatic headlight
            # ------------------------------------------------------------------
            try:
                # LightKit APIs are not present in some VTK builds; avoid calling them.
                # Instead, explicitly disable automatic headlight so scene lights are used.
                if hasattr(renderer, "SetAutomaticLightCreation"):
                    try:
                        renderer.SetAutomaticLightCreation(0)
                    except Exception:
                        # Some builds expose different names; ignore failures
                        pass
                else:
                    # older API names (best-effort)
                    try:
                        renderer.AutomaticLightCreationOff()
                    except Exception:
                        pass
            except Exception:
                pass

            # Ensure renderWindow multisampling is enabled where possible (helps depth peeling)
            try:
                rw = view.renderWindow()
                try:
                    rw.SetMultiSamples(8)
                except Exception:
                    # Not all builds allow changing multisamples at runtime; ignore
                    pass
            except Exception:
                rw = None

            # Remove all existing lights to prevent double-stacking on cold startup
            try:
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
            except Exception:
                pass

            # Remove default headlight if present (safe sweep)
            try:
                lights = renderer.GetLights()
                lights.InitTraversal()
                head = lights.GetNextItem()
                if head:
                    try:
                        renderer.RemoveLight(head)
                    except Exception:
                        pass
            except Exception:
                pass

            viewLights = []

            # Rim light
            try:
                rim = vtk.vtkLight()
                rim.SetLightTypeToSceneLight()
                rim.SetPosition(-140, -310, 210)
                rim.SetFocalPoint(0, 0, 0)
                rim.SetColor(0.60, 0.70, 1.00)
                # safer default to preserve contrast
                rim.SetIntensity(0.45)
                renderer.AddLight(rim)
                viewLights.append(rim)
                try:
                    rim.SetAttribute("EPCMR_LightRole", "rim")
                except Exception:
                    pass
            except Exception:
                pass

            # Fill light
            try:
                fill = vtk.vtkLight()
                fill.SetLightTypeToSceneLight()
                fill.SetPosition(0, 300, 120)
                fill.SetFocalPoint(0, 0, 0)
                fill.SetColor(1.00, 0.85, 0.70)
                # moderate fill to add mid-tone illumination
                fill.SetIntensity(0.55)
                renderer.AddLight(fill)
                viewLights.append(fill)
                try:
                    fill.SetAttribute("EPCMR_LightRole", "fill")
                except Exception:
                    pass
            except Exception:
                pass

            # Catheter lights
            try:
                catFront = vtk.vtkLight()
                catFront.SetLightTypeToSceneLight()
                catFront.SetPosition(120, -80, 180)
                catFront.SetFocalPoint(0, 0, 0)
                catFront.SetColor(1.00, 1.00, 1.00)
                # slightly reduced from previous extreme to avoid flattening
                catFront.SetIntensity(1.2)
                renderer.AddLight(catFront)
                viewLights.append(catFront)
                try:
                    catFront.SetAttribute("EPCMR_LightRole", "catFront")
                except Exception:
                    pass
            except Exception:
                catFront = None

            try:
                catRear = vtk.vtkLight()
                catRear.SetLightTypeToSceneLight()
                catRear.SetPosition(-120, 80, -160)
                catRear.SetFocalPoint(0, 0, 0)
                catRear.SetColor(1.00, 1.00, 1.00)
                catRear.SetIntensity(0.9)
                renderer.AddLight(catRear)
                viewLights.append(catRear)
                try:
                    catRear.SetAttribute("EPCMR_LightRole", "catRear")
                except Exception:
                    pass
            except Exception:
                catRear = None

            try:
                catTop = vtk.vtkLight()
                catTop.SetLightTypeToSceneLight()
                catTop.SetPosition(0, 0, 260)
                catTop.SetFocalPoint(0, 0, 0)
                catTop.SetColor(1.00, 1.00, 1.00)
                catTop.SetIntensity(0.8)
                renderer.AddLight(catTop)
                viewLights.append(catTop)
                try:
                    catTop.SetAttribute("EPCMR_LightRole", "catTop")
                except Exception:
                    pass
            except Exception:
                catTop = None

            for catLight in (catFront, catRear, catTop):
                try:
                    if catLight:
                        catLight.SetAmbientColor(1.0, 1.0, 1.0)
                        catLight.SetDiffuseColor(1.0, 1.0, 1.0)
                        catLight.SetSpecularColor(1.0, 1.0, 1.0)
                except Exception:
                    pass

            # Ambient anatomy light
            try:
                ambient = vtk.vtkLight()
                ambient.SetLightTypeToSceneLight()
                ambient.SetPosition(0, 0, 0)
                ambient.SetFocalPoint(0, 0, 0)
                ambient.SetColor(0.95, 0.95, 1.00)
                # reduced ambient to avoid flattening contrast
                ambient.SetIntensity(0.6)
                renderer.AddLight(ambient)
                viewLights.append(ambient)
                try:
                    ambient.SetAttribute("EPCMR_LightRole", "ambient")
                except Exception:
                    pass
            except Exception:
                pass

            # Under-light
            try:
                under = vtk.vtkLight()
                under.SetLightTypeToSceneLight()
                under.SetPosition(0, -400, -200)
                under.SetFocalPoint(0, 0, 0)
                under.SetColor(0.85, 0.90, 1.00)
                # intentionally weak under-light to avoid backlight wash
                under.SetIntensity(0.35)
                renderer.AddLight(under)
                viewLights.append(under)
                try:
                    under.SetAttribute("EPCMR_LightRole", "under")
                except Exception:
                    pass
            except Exception:
                pass

            # No-shadow ambient light
            try:
                noShadow = vtk.vtkLight()
                noShadow.SetLightTypeToSceneLight()
                noShadow.SetPosition(0, 0, 0)
                noShadow.SetFocalPoint(0, 0, 0)
                noShadow.SetColor(1.0, 1.0, 1.0)
                # clamp no-shadow to a conservative value
                noShadow.SetIntensity(0.45)
                # Use ambient/diffuse/specular color setters if available
                try:
                    noShadow.SetAmbientColor(1.0, 1.0, 1.0)
                except Exception:
                    pass
                try:
                    noShadow.SetDiffuseColor(0.0, 0.0, 0.0)
                except Exception:
                    pass
                try:
                    noShadow.SetSpecularColor(0.0, 0.0, 0.0)
                except Exception:
                    pass
                renderer.AddLight(noShadow)
                viewLights.append(noShadow)
                try:
                    noShadow.SetAttribute("EPCMR_LightRole", "noShadow")
                except Exception:
                    pass
            except Exception:
                pass

            # apply global scale to the lights we just created
            # NOTE: _globalLightScale is the master brightness control
            # (ie. adjust global brightness without touching individual light roles or material parameters)
            # ---------------------------------------------------------------------------
            # Global Light Scale Reference
            # ---------------------------------------------------------------------------
            # self._globalLightScale = 0.7   -> darker anatomy
            # self._globalLightScale = 1.0   -> baseline brightness
            # self._globalLightScale = 1.3   -> brighter anatomy
            # self._globalLightScale = 1.6   -> strong illumination
            # ---------------------------------------------------------------------------

            try:
                self._globalLightScale = getattr(self, "_globalLightScale", 1.1)
                scale = float(self._globalLightScale)
                if scale != 1.0:
                    for l in viewLights:
                        try:
                            try:
                                current = l.GetIntensity()
                                l.SetIntensity(max(0.0, current * scale))
                            except Exception:
                                l.SetIntensity(1.0 * scale)
                        except Exception:
                            pass
            except Exception:
                pass

            # deterministic clamp: ensure under/noShadow/central lights cannot flatten contrast
            try:
                try:
                    ren = view.renderWindow().GetRenderers().GetFirstRenderer()
                except Exception:
                    ren = None
                if ren:
                    lightsClamp = ren.GetLights()
                    lightsClamp.InitTraversal()
                    lClamp = lightsClamp.GetNextItem()
                    while lClamp:
                        try:
                            role = None
                            try:
                                role = lClamp.GetAttribute("EPCMR_LightRole")
                            except Exception:
                                pass
                            # clamp by explicit role
                            if role == "under":
                                try:
                                    lClamp.SetIntensity(min(lClamp.GetIntensity(), 0.35))
                                except Exception:
                                    pass
                            elif role == "noShadow":
                                try:
                                    lClamp.SetIntensity(min(lClamp.GetIntensity(), 0.45))
                                except Exception:
                                    pass
                            elif role == "ambient":
                                try:
                                    lClamp.SetIntensity(min(lClamp.GetIntensity(), 0.6))
                                except Exception:
                                    pass
                            elif role == "catTop":
                                try:
                                    lClamp.SetIntensity(min(lClamp.GetIntensity(), 0.8))
                                except Exception:
                                    pass
                            else:
                                # fallback heuristic: clamp lights at or near origin or high Z
                                try:
                                    pos = lClamp.GetPosition()
                                    if pos and (pos == (0.0, 0.0, 0.0) or pos == (0.0, 0.0, 260.0)):
                                        lClamp.SetIntensity(min(lClamp.GetIntensity(), 0.85))
                                except Exception:
                                    pass
                        except Exception:
                            pass
                        lClamp = lightsClamp.GetNextItem()
            except Exception:
                pass

            self._lightsPerView[vid] = viewLights

            # Renderer quality settings
            try:
                # FXAA and depth peeling can conflict; disable FXAA when using depth peeling
                # Prefer API variants depending on VTK build
                try:
                    if hasattr(renderer, "UseFXAAOff"):
                        renderer.UseFXAAOff()
                    elif hasattr(renderer, "SetUseFXAA"):
                        renderer.SetUseFXAA(0)
                except Exception:
                    # ignore FXAA toggle failures
                    pass

                # Enable depth peeling only if multisampling is available or if enabling succeeds
                try:
                    if hasattr(renderer, "SetUseDepthPeeling"):
                        # If multisampling was set on the renderWindow above, depth peeling is more likely to work
                        renderer.SetUseDepthPeeling(1)
                        try:
                            renderer.SetMaximumNumberOfPeels(50)
                        except Exception:
                            pass
                        try:
                            renderer.SetOcclusionRatio(0.1)
                        except Exception:
                            pass
                except Exception:
                    # If depth peeling fails, disable it to avoid VTK disabling lights silently
                    try:
                        if hasattr(renderer, "SetUseDepthPeeling"):
                            renderer.SetUseDepthPeeling(0)
                    except Exception:
                        pass
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

        # Slight renderer ambient to ensure diffuse surfaces remain visible, but clamp to avoid flattening
        try:
            ren = (
                slicer.app.layoutManager().threeDWidget(0).threeDView().renderWindow().GetRenderers().GetFirstRenderer()
            )
            try:
                # clamp ambient to a moderate value
                currentAmbient = None
                try:
                    currentAmbient = ren.GetAmbient()
                except Exception:
                    pass
                # prefer explicit set to a safe default if GetAmbient is unavailable
                try:
                    if currentAmbient and isinstance(currentAmbient, tuple):
                        # clamp each channel
                        r = min(currentAmbient[0], 0.25)
                        g = min(currentAmbient[1], 0.25)
                        b = min(currentAmbient[2], 0.25)
                        ren.SetAmbient(r, g, b)
                    else:
                        ren.SetAmbient(0.20, 0.20, 0.20)
                except Exception:
                    # final fallback
                    try:
                        ren.SetAmbient(0.20, 0.20, 0.20)
                    except Exception:
                        pass
            except Exception:
                pass
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

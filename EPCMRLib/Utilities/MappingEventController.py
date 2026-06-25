# ruff: isort: skip_file
import logging
import slicer


class MappingEventController:
    """
    Centralized controller for mapping-related markups events.

    Responsibilities:
      - Attach MRML observers to mappingPts.
      - Provide a single, deterministic dispatcher for:
          - PointAddedEvent
          - PointModifiedEvent
          - PointRemovedEvent
      - Drive:
          - RACloneManager neutral/gray state
          - GeometryInterpolator.run()
          - SceneManager.updateRightAtrialColormap()
      - Ensure:
          - 0 points -> cyan RA, legends hidden
          - 1 point  -> gray RA, legends hidden
          - >=2      -> interpolated RA, legends shown

    Restore-safety:
      - During backup restore, isRestoringBackup is set on this controller.
      - All interpolation + legend updates are suppressed while this flag is True.
      - SceneManager handles a single final legend update after restore.
    """

    def __init__(self, pNode, raManager, interpolator, sceneManager):
        """
        pNode:
            EPCMRParameterNode wrapper.
        raManager:
            RACloneManager instance.
        interpolator:
            GeometryInterpolator instance.
        sceneManager:
            SceneManager instance (for legends).
        """
        self.pNode = pNode
        self.raManager = raManager
        self.interpolator = interpolator
        self.sceneManager = sceneManager

        self._observerTags = {}
        self.mappingNode = None

        # Restore-safety flag (set by logic during backup restore)
        self.isRestoringBackup = False

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
    def set_mapping_node(self, markupsNode):
        """
        Attach observers to the mappingPts node.
        """
        if not markupsNode:
            return

        self.mappingNode = markupsNode
        self._attachNodeObservers(markupsNode)

    def cleanupObservers(self):
        """
        Detach all observers. Call this on module reload / cleanup.
        """
        for nodeId, tags in self._observerTags.items():
            node = slicer.mrmlScene.GetNodeByID(nodeId)
            if node:
                for tag in tags:
                    node.RemoveObserver(tag)
        self._observerTags = {}

    # ------------------------------------------------------------------
    # Internal: observer wiring
    # ------------------------------------------------------------------
    def _attachNodeObservers(self, markupsNode):
        """
        Attach PointAdded/Removed/Modified observers to a markups node.
        """
        if not markupsNode:
            return

        nodeId = markupsNode.GetID()
        if nodeId in self._observerTags:
            return

        tags = []

        tags.append(
            markupsNode.AddObserver(
                slicer.vtkMRMLMarkupsNode.PointAddedEvent, lambda caller, event: self.on_point_added(caller)
            )
        )
        tags.append(
            markupsNode.AddObserver(
                slicer.vtkMRMLMarkupsNode.PointRemovedEvent, lambda caller, event: self.on_point_removed(caller)
            )
        )
        tags.append(
            markupsNode.AddObserver(
                slicer.vtkMRMLMarkupsNode.PointModifiedEvent, lambda caller, event: self.on_point_modified(caller)
            )
        )

        self._observerTags[nodeId] = tags
        logging.debug(f"MappingEventController: observers attached to {markupsNode.GetName()}")

    # ------------------------------------------------------------------
    # Shared helper
    # ------------------------------------------------------------------
    def _handle_point_count_state(self, caller) -> bool:
        """
        Apply RA neutral/gray state and legend visibility based on point count.

        Returns:
            True  -> state handled fully (0 or 1 point, or suppressed), caller must RETURN
            False -> n >= 2, caller must proceed with heatmap + interpolation

        This function must never return None. All code paths return a bool.
        """

        # Suppress interpolation during replay or mode switching
        if getattr(self.pNode, "replayModeActive", False):
            return True

        if getattr(self.pNode, "modeSwitchInProgress", False):
            return True

        # Suppress all mapping reactions during backup restore
        if getattr(self, "isRestoringBackup", False):
            return True

        # Number of mapping points
        n = caller.GetNumberOfControlPoints()

        # ------------------------------------------------------------------
        # 0 POINTS -> cyan RA, clone hidden, legends hidden
        # ------------------------------------------------------------------
        if n <= 0:
            self.raManager.set_neutral_state(num_points=0)
            try:
                self.sceneManager._hideActivationScalarBar()
                self.sceneManager._hideVoltageScalarBar()
                slicer.util.forceRenderAllViews()
            except Exception as e:
                logging.error(f"_handle_point_count_state (n=0): {e}")
            return True

        # ------------------------------------------------------------------
        # 1 POINT -> gray clone, RA hidden, legends hidden
        # ------------------------------------------------------------------
        if n == 1:
            self.raManager.set_neutral_state(num_points=1)
            try:
                self.sceneManager._hideActivationScalarBar()
                self.sceneManager._hideVoltageScalarBar()
                slicer.util.forceRenderAllViews()
            except Exception as e:
                logging.error(f"_handle_point_count_state (n=1): {e}")
            return True

        # ------------------------------------------------------------------
        # >= 2 POINTS -> heatmap mode (caller must run interpolation)
        # ------------------------------------------------------------------
        return False

    def _sync_markups(self, markupsNode):
        """
        Force Slicer to commit pending control point updates before we
        read counts/coordinates/labels. Critical when points are added
        programmatically (shortcuts, scripts).
        """
        if not markupsNode:
            return 0

        # Force MRML update + event processing
        markupsNode.Modified()
        slicer.app.processEvents()

        return markupsNode.GetNumberOfControlPoints()

    # ------------------------------------------------------------------
    # Event handlers
    # ------------------------------------------------------------------
    def on_point_added(self, caller):
        """
        Handle PointAddedEvent.

        Behavior:
        - 0 points -> cyan RA, legends hidden
        - 1 point  -> gray RA, legends hidden
        - >=2      -> heatmap mode + interpolation + legend update

        IMPORTANT:
        - Markups must be synchronized before reading counts/coordinates,
            because points added via shortcuts fire events before the
            internal control point table is fully updated.
        - Voltage Mapping MUST run interpolation immediately when points
            are placed. Without this, the RA clone will not update until a
            UI slider fires a separate event.

        Restore-safety:
        - When isRestoringBackup is True, this handler becomes a no-op
          beyond neutral-state handling in _handle_point_count_state().
        """

        # Ensure markups are fully committed before any processing
        self._sync_markups(caller)

        # Handle 0/1-point neutral states or restore suppression; returns True if handled
        if self._handle_point_count_state(caller):
            return

        try:
            # Ensure heatmap mode is active for >=2 points
            self.raManager.set_heatmap_state()

            # Always run interpolation for >=2 points
            self.interpolator.run(caller)

            # Always update legend after interpolation
            self.sceneManager.updateRightAtrialColormap()

        except Exception as e:
            logging.error(f"on_point_added: interpolation or legend update failed: {e}")
            import traceback

            traceback.print_exc()

    def on_point_removed(self, caller):
        """
        Handle PointRemovedEvent.

        Behavior:
        - 0 points -> cyan RA, legends hidden
        - 1 point  -> gray RA, legends hidden
        - >=2      -> run interpolation and update legend

        IMPORTANT:
        Markups must be synchronized before reading counts/coordinates,
        because removal events can also fire before the internal control
        point table is fully updated.

        Restore-safety:
        - When isRestoringBackup is True, this handler becomes a no-op
          beyond neutral-state handling in _handle_point_count_state().
        """

        # Ensure markups are fully committed before any processing
        self._sync_markups(caller)

        if self._handle_point_count_state(caller):
            return

        try:
            self.interpolator.run(caller)
            self.sceneManager.updateRightAtrialColormap()
        except Exception as e:
            logging.error(f"on_point_removed: interpolation or legend update failed: {e}")
            import traceback

            traceback.print_exc()

    def on_point_modified(self, caller):
        """
        Handle PointModifiedEvent.

        Behavior:
        - 0 points -> cyan RA, legends hidden
        - 1 point  -> gray RA, legends hidden
        - >=2      -> heatmap mode + interpolation + legend update

        IMPORTANT:
        - Markups must be synchronized before reading counts/coordinates,
            because modification events can fire while the last point is
            still in a transient state.
        - In both Activation Time Mapping and Voltage Mapping,
            interpolation MUST run immediately when mapping points change.
            This ensures that placing or moving mapping points directly
            updates the RA clone heatmap without requiring UI slider changes.

        Restore-safety:
        - When isRestoringBackup is True, this handler becomes a no-op
          beyond neutral-state handling in _handle_point_count_state().
        """

        # Ensure markups are fully committed before any processing
        self._sync_markups(caller)

        # Handle 0/1-point neutral states or restore suppression; returns True if handled
        if self._handle_point_count_state(caller):
            return

        try:
            # Ensure heatmap mode is active for >=2 points
            self.raManager.set_heatmap_state()

            # Always run interpolation for >=2 points
            self.interpolator.run(caller)

            # Always update legend after interpolation
            self.sceneManager.updateRightAtrialColormap()

        except Exception as e:
            logging.error(f"on_point_modified: interpolation or legend update failed: {e}")
            import traceback

            traceback.print_exc()

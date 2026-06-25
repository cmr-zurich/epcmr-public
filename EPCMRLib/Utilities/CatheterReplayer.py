# v3.1 - CatheterReplayer with high-precision timing, FPS, and robust visibility
from __future__ import annotations

import json
import logging
import os
from typing import TYPE_CHECKING, Any, Dict, List, Optional

import numpy as np
import qt
import slicer
import vtk

if TYPE_CHECKING:
    from EPCMRLib.EPCMRParameterNode import EPCMRParameterNode


class CatheterReplayerUI(qt.QWidget):
    def __init__(self, replayer_instance: "CatheterReplayer", parent=None):
        super().__init__(parent or slicer.util.mainWindow())
        self.replayer = replayer_instance

        self.replayer.ui = self
        self.on_closed_callback = None
        self.setWindowFlags(qt.Qt.Window | qt.Qt.WindowStaysOnTopHint | qt.Qt.Tool)

        self.setup_ui()

        # Ensure correct LIVE styling after UI is fully realized
        qt.QTimer.singleShot(0, lambda: self.replayer.set_mode_replay(False))

    def setup_ui(self) -> None:
        self.setWindowTitle("Catheter Replay Controls")
        self.setMinimumWidth(650)
        self.setAttribute(qt.Qt.WA_QuitOnClose, False)
        layout = qt.QVBoxLayout(self)

        # 1. Mode Button
        self.mode_button = qt.QPushButton("MODE: LIVE")
        self.mode_button.setCheckable(True)
        self.mode_button.setFixedHeight(50)
        self.mode_button.setFocusPolicy(qt.Qt.NoFocus)
        self.mode_button.toggled.connect(self.replayer.set_mode_replay)
        layout.addWidget(self.mode_button)

        # 2. File Selection
        file_layout = qt.QHBoxLayout()
        self.open_button = qt.QPushButton("? Open .jsonl File")
        self.open_button.clicked.connect(self.replayer.on_open_file)
        self.file_label = qt.QLabel("No file selected")
        self.file_label.setStyleSheet("font-style: italic; color: #888888;")
        file_layout.addWidget(self.open_button)
        file_layout.addWidget(self.file_label)
        layout.addLayout(file_layout)

        # 3. Info Labels (frame + time + FPS)
        info_layout = qt.QHBoxLayout()
        self.info_label = qt.QLabel("Frame: 0 / 0")
        self.info_label.setStyleSheet("font-weight: bold; font-family: monospace;")
        info_layout.addWidget(self.info_label)

        self.fps_label = qt.QLabel("FPS: 0.0")
        self.fps_label.setStyleSheet("font-family: monospace; color: #CCCCCC; margin-left: 12px;")
        info_layout.addWidget(self.fps_label)

        info_layout.addStretch()
        layout.addLayout(info_layout)

        # 4. Timeline Slider
        self.slider = qt.QSlider(qt.Qt.Horizontal)
        self.slider.valueChanged.connect(self.replayer.jump_to_frame)
        layout.addWidget(self.slider)

        # 5. Playback Speed + Mode
        speed_mode_layout = qt.QHBoxLayout()

        # Speed combo
        self.speed_combo = qt.QComboBox()
        self.speed_combo.setObjectName("speed_combo")
        self.speed_combo.currentIndexChanged.connect(lambda _: self.replayer.update_timer_speed())
        self.speed_combo.addItems(["0.5x", "1.0x", "2.0x", "5.0x"])
        self.speed_combo.setCurrentIndex(1)

        speed_mode_layout.addWidget(qt.QLabel("Speed:"))
        speed_mode_layout.addWidget(self.speed_combo)

        # Playback mode combo (frame-accurate vs real-time)
        self.mode_combo = qt.QComboBox()
        self.mode_combo.setObjectName("playback_mode_combo")
        self.mode_combo.addItems(["Frame-accurate", "Real-time"])
        self.mode_combo.setCurrentIndex(0)
        self.mode_combo.currentIndexChanged.connect(self.replayer.on_playback_mode_changed)

        speed_mode_layout.addSpacing(20)
        speed_mode_layout.addWidget(qt.QLabel("Timing:"))
        speed_mode_layout.addWidget(self.mode_combo)

        speed_mode_layout.addStretch()
        layout.addLayout(speed_mode_layout)

        # 6. Playback Controls
        btn_layout = qt.QHBoxLayout()
        self.btn_first = qt.QPushButton("<<")
        self.btn_prev = qt.QPushButton("<")
        self.btn_rev = qt.QPushButton("Play Rev")
        self.btn_rev.setCheckable(True)
        self.btn_play_stop = qt.QPushButton("Play Fwd")
        self.btn_play_stop.setCheckable(True)
        self.btn_play_stop.setStyleSheet("font-weight: bold; background-color: #e1f5fe;")
        self.btn_next = qt.QPushButton(">")
        self.btn_last = qt.QPushButton(">>")

        self.btn_first.clicked.connect(lambda: self.replayer.jump_to_frame(0))
        self.btn_last.clicked.connect(
            lambda: self.replayer.jump_to_frame(max(0, len(self.replayer.matrix_history) - 1))
        )
        self.btn_prev.clicked.connect(lambda: self.replayer.jump_to_frame(self.replayer.current_idx - 1))
        self.btn_next.clicked.connect(lambda: self.replayer.jump_to_frame(self.replayer.current_idx + 1))

        self.btn_play_stop.clicked.connect(lambda: self.replayer.toggle_playback(1))
        self.btn_rev.clicked.connect(lambda: self.replayer.toggle_playback(-1))

        self.playback_buttons = [
            self.btn_first,
            self.btn_prev,
            self.btn_rev,
            self.btn_play_stop,
            self.btn_next,
            self.btn_last,
        ]

        for b in self.playback_buttons:
            b.setFocusPolicy(qt.Qt.NoFocus)
            btn_layout.addWidget(b)

        layout.addLayout(btn_layout)

    def closeEvent(self, event: qt.QCloseEvent) -> None:
        if self.replayer:
            self.replayer.cleanup()
        if self.on_closed_callback:
            self.on_closed_callback()
        event.accept()


class CatheterReplayer:
    """
    Core replay engine for EPCMR.

    Responsibilities:
      - Load JSONL recordings and build Abl-driven replay bundles.
      - Maintain per-frame transform matrices and validity flags.
      - Drive LIVE vs REPLAY transform routing for Abl_01 and Ref_01.
      - Coordinate with SceneManager for per-catheter tinting.
      - Provide high-precision timing, FPS measurement, and two timing modes:
          * Frame-accurate (index-based stepping)
          * Real-time (wall-clock aligned to timestamps)
    """

    KEYS = ["Abl_01", "Ref_01"]

    def __init__(self, pNode: "EPCMRParameterNode", sceneManager=None):
        self.pNode: EPCMRParameterNode = pNode
        self.sceneManager = sceneManager

        # Model cache (will be resolved in setup_nodes)
        self.models: Dict[str, Optional[vtk.vtkMRMLNode]] = {
            "Abl_01": getattr(self.pNode, "ablModel", None),
            "Ref_01": getattr(self.pNode, "refModel", None),
        }

        self.replay_transforms: Dict[str, Optional[vtk.vtkMRMLLinearTransformNode]] = {}
        self.live_transforms: Dict[str, Optional[vtk.vtkMRMLLinearTransformNode]] = {}
        self.matrix_history: List[Dict[str, Any]] = []
        self.current_idx: int = 0
        self.play_direction: int = 1

        # Timing
        self.start_timestamp: Optional[float] = None  # first timestamp in recording
        self.native_interval_ms: int = 100  # estimated from data
        self._speed_factor: float = 1.0  # from speed combo
        self._playbackMode: str = "frame"  # "frame" or "realtime"

        # High-precision timer for real-time mode
        self._elapsedTimer = qt.QElapsedTimer()
        self._replayStartWallMs: Optional[int] = None

        # FPS measurement
        self._fpsFrameCount: int = 0
        self._fpsLastUpdateMs: Optional[int] = None
        self._currentFPS: float = 0.0

        # Debug / info
        self.last_line_abl: Optional[int] = None
        self.last_line_ref: Optional[int] = None

        # Qt timer for driving playback
        self.timer = qt.QTimer()
        self.timer.timeout.connect(self.process_auto_play)

        self.ui: Optional[CatheterReplayerUI] = None

        # Guard only for transform wiring, not for model resolution
        self._nodesInitialized: bool = False

        self.setup_nodes()

        # Create UI lazily but immediately for now
        self.ui = CatheterReplayerUI(self)
        self.ui.mode_button.setChecked(False)
        self.set_mode_replay(False)

    # ------------------------------------------------------------------
    # Backwards-compatible UI entry point
    # ------------------------------------------------------------------
    def show_ui(self):
        """
        Backwards-compatible entry point for RAFlutterWidget.
        Ensures the UI window is shown and brought to front.
        """
        if self.ui:
            self.ui.show()
            self.ui.raise_()
            self.ui.activateWindow()
        return self.ui

    # ------------------------------------------------------------------
    # Node / transform setup
    # ------------------------------------------------------------------
    def _resolve_models_from_scene(self) -> None:
        """
        Ensure self.models and pNode.*Model always point to real scene nodes.
        This is safe to call multiple times (idempotent).
        """
        abl_model = getattr(self.pNode, "ablModel", None)
        ref_model = getattr(self.pNode, "refModel", None)

        if abl_model is None:
            abl_model = slicer.util.getFirstNodeByName("Abl_01_Model")
        if ref_model is None:
            ref_model = slicer.util.getFirstNodeByName("Ref_01_Model")

        self.models = {
            "Abl_01": abl_model,
            "Ref_01": ref_model,
        }

        self.pNode.ablModel = abl_model
        self.pNode.refModel = ref_model

    def setup_nodes(self) -> None:
        """
        One-time transform wiring + always-up-to-date model resolution.
        Models are re-resolved on every call; transforms only once.
        """
        if not self.pNode:
            return

        self._resolve_models_from_scene()

        if self._nodesInitialized:
            return

        mapping = {
            "Abl_01": ("ablTransform", "ablReplayTransform", "Abl"),
            "Ref_01": ("refTransform", "refReplayTransform", "Ref"),
        }

        for key, (live_attr, replay_attr, base) in mapping.items():
            # LIVE transform
            live_name = f"{base}_01_TF"
            ltf = slicer.util.getFirstNodeByName(live_name)
            if not ltf:
                ltf = slicer.mrmlScene.AddNewNodeByClass("vtkMRMLLinearTransformNode", live_name)

            setattr(self.pNode, live_attr, ltf)
            self.live_transforms[key] = ltf

            # REPLAY transform
            replay_name = f"{base}_01_REPLAY_TF"
            rtf = slicer.util.getFirstNodeByName(replay_name)
            if not rtf:
                rtf = slicer.mrmlScene.AddNewNodeByClass("vtkMRMLLinearTransformNode", replay_name)
                rtf.SetSaveWithScene(False)

            setattr(self.pNode, replay_attr, rtf)
            self.replay_transforms[key] = rtf

            # Initial binding: models -> LIVE transform (when not in replay mode)
            model = self.models.get(key)
            if model:
                rtf.SetAttribute("TargetModelID", model.GetID())
                if not getattr(self.pNode, "replayModeActive", False):
                    model.SetAndObserveTransformNodeID(ltf.GetID())
                model.Modified()

        self._nodesInitialized = True

    # ------------------------------------------------------------------
    # Mode switching (LIVE / REPLAY)
    # ------------------------------------------------------------------
    def set_mode_replay(self, replay_active: bool) -> None:
        """
        Switch between LIVE and REPLAY modes.

        Critical:
          - SceneManager.updateCatheterVisuals() only runs in replay mode if
            pNode.replayerActive == True.
          - Therefore we must explicitly drive pNode.replayerActive here.
        """
        if not self.pNode:
            return

        self.setup_nodes()

        if self.timer is None:
            self.timer = qt.QTimer()
            self.timer.timeout.connect(self.process_auto_play)

        self.pNode.replayModeActive = bool(replay_active)
        self.pNode.replayerActive = bool(replay_active)

        if self.ui:
            self.timer.stop()
            self.ui.btn_play_stop.setChecked(False)
            self.ui.btn_play_stop.setText("Play Fwd")
            self.ui.btn_rev.setChecked(False)
            self.ui.btn_rev.setText("Play Rev")

            fwd_idle = "QPushButton { font-weight: bold; background-color: #e1f5fe; color: black; }"
            rev_idle = "QPushButton { font-weight: normal; color: black; }"
            self.ui.btn_rev.setStyleSheet(rev_idle)

            for child in self.ui.findChildren(qt.QWidget):
                if child != self.ui.mode_button:
                    child.setEnabled(replay_active)

            if replay_active:
                self.ui.btn_play_stop.setStyleSheet(fwd_idle)
            else:
                self.ui.btn_play_stop.setStyleSheet(
                    "QPushButton { font-weight: bold; background-color: #e1f5fe; color: gray; }"
                )

        mw = slicer.util.mainWindow()
        if mw:
            for s in mw.findChildren(qt.QShortcut):
                try:
                    key_str = s.key().toString().upper()
                except Exception:
                    continue
                if key_str in ["D", "M"]:
                    s.setEnabled(True)

        try:
            from EPCMRLib.Utilities.SceneManager import SceneManager

            manager = SceneManager(self.pNode)
        except ImportError:
            manager = None

        mapping = {
            "Abl_01": ("ablModel", "ablTransform", "ablReplayTransform"),
            "Ref_01": ("refModel", "refTransform", "refReplayTransform"),
        }

        for _, (model_attr, live_attr, replay_attr) in mapping.items():
            model = getattr(self.pNode, model_attr, None)
            live_tf = getattr(self.pNode, live_attr, None)
            replay_tf = getattr(self.pNode, replay_attr, None)

            if not model:
                continue

            if replay_active:
                # REPLAY: bind model to *_REPLAY_TF
                if replay_tf:
                    if model.GetTransformNodeID() != replay_tf.GetID():
                        model.SetAndObserveTransformNodeID(replay_tf.GetID())
                    replay_tf.SetAttribute("TargetModelID", model.GetID())

                if live_tf:
                    live_tf.SetAttribute("ReplayModeActive", "True")

                # Ensure catheters are visible when entering replay
                dn = model.GetDisplayNode()
                if dn:
                    dn.SetVisibility(True)
                    dn.SetVisibility2D(True)
                    dn.SetOpacity(1.0)
                    dn.Modified()

                if manager:
                    manager.updateCatheterVisuals(model, True)

            else:
                # LIVE: leaving replay -> disable replayerActive
                if hasattr(self.pNode, "replayerActive"):
                    self.pNode.replayerActive = False

                try:
                    widget = slicer.modules.epcmr.widgetRepresentation().self()
                    if hasattr(widget, "shortcutManager") and widget.shortcutManager:
                        widget.shortcutManager.syncParameterNode()
                        widget.shortcutManager.setEnabled(True)
                        slicer.util.mainWindow().setFocus(qt.Qt.ActiveWindowFocusReason)
                        logging.info("CatheterReplayer: Shortcuts restored in LIVE mode.")
                except Exception as e:
                    logging.error(f"CatheterReplayer: Failed to restore shortcuts in LIVE mode: {e}")

                if live_tf:
                    if model.GetTransformNodeID() != live_tf.GetID():
                        model.SetAndObserveTransformNodeID(live_tf.GetID())
                    live_tf.SetAttribute("ReplayModeActive", "False")
                    live_tf.SetAttribute("NeedsInit", "True")
                    live_tf.SetAttribute("LastKnownValidState", "HandoverReset")

                if manager:
                    manager.updateCatheterVisuals(model, True)

            model.Modified()

        anno_text = "REPLAY" if replay_active else "LIVE"
        anno_color = (0.98, 0.75, 0.18) if replay_active else (0.2, 1.0, 0.2)

        layoutManager = slicer.app.layoutManager()
        if layoutManager:
            for i in range(layoutManager.threeDViewCount):
                threeDWidget = layoutManager.threeDWidget(i)
                if not threeDWidget:
                    continue
                view = threeDWidget.threeDView()
                if not view:
                    continue
                cornerAnnotation = view.cornerAnnotation()
                if cornerAnnotation:
                    cornerAnnotation.SetText(vtk.vtkCornerAnnotation.UpperRight, anno_text)
                    prop = cornerAnnotation.GetTextProperty()
                    if prop:
                        prop.SetColor(*anno_color)
                        prop.SetFontFamilyToArial()
                        prop.BoldOn()
                        prop.SetFontSize(18)

        if self.ui:
            if replay_active:
                self.ui.mode_button.setText("MODE: REPLAY")
                self.ui.mode_button.setStyleSheet(
                    "background-color: #fff9c4; color: #fbc02d; font-weight: bold; border: 2px solid #fbc02d;"
                )
                self.jump_to_frame(self.current_idx)
            else:
                self.ui.mode_button.setText("MODE: LIVE")
                self.ui.mode_button.setStyleSheet(
                    "background-color: #c8e6c9; color: #2e7d32; font-weight: bold; border: 2px solid #2e7d32;"
                )

        slicer.util.forceRenderAllViews()

    # ------------------------------------------------------------------
    # Playback mode change (frame / realtime)
    # ------------------------------------------------------------------
    def on_playback_mode_changed(self, index: int) -> None:
        if not self.ui:
            return
        text = self.ui.mode_combo.currentText
        if "Real-time" in text:
            self._playbackMode = "realtime"
        else:
            self._playbackMode = "frame"
        logging.info(f"CatheterReplayer: Playback mode set to {self._playbackMode}")

    # ------------------------------------------------------------------
    # File loading
    # ------------------------------------------------------------------
    def on_open_file(self) -> None:
        """Open a JSONL recording, parse it into bundles, and initialize replay state."""
        if not self.ui:
            return

        file_path = qt.QFileDialog.getOpenFileName(self.ui, "Open Recording", "", "JSONL Files (*.jsonl)")
        if not file_path:
            return

        try:
            self.matrix_history = []
            self.current_idx = 0
            self.start_timestamp = None
            self.last_line_abl = None
            self.last_line_ref = None
            self.native_interval_ms = 100
            self.pNode.validityTable = {}
            if hasattr(self.pNode, "replayerActive"):
                self.pNode.replayerActive = False

            self.setup_nodes()

            for key in self.KEYS:
                tf = self.replay_transforms.get(key)
                model = self.models.get(key)
                if tf is not None:
                    tf.SetAttribute("OpenIGTLink.valid", "true")
                if model is not None:
                    dn = model.GetDisplayNode()
                    if dn is not None:
                        dn.SetOpacity(1.0)
                        dn.SetColor(0.2, 1.0, 0.2)
                        dn.Modified()

            buffer = ""
            json_index = 0
            abl_list: List[Dict[str, Any]] = []
            ref_list: List[Dict[str, Any]] = []

            with open(file_path, "r", encoding="utf-8") as f:
                for line in f:
                    buffer += line
                    try:
                        data = json.loads(buffer)
                        buffer = ""
                    except json.JSONDecodeError:
                        continue

                    raw_name = data.get("device_name", "")
                    clean_key = raw_name.replace("_TF", "")

                    if clean_key not in self.KEYS:
                        continue

                    ts = data.get("timestamp", 0.0)
                    if ts is not None and self.start_timestamp is None:
                        self.start_timestamp = ts

                    entry = {
                        "data": data,
                        "json_index": json_index,
                        "timestamp": ts,
                    }
                    json_index += 1

                    if clean_key == "Abl_01":
                        abl_list.append(entry)
                    elif clean_key == "Ref_01":
                        ref_list.append(entry)

            bundles: List[Dict[str, Any]] = []
            ref_indices = [ref["json_index"] for ref in ref_list]

            def parse_valid_flag(d: Dict[str, Any]) -> bool:
                raw = d.get("valid", True)
                if isinstance(raw, str):
                    return raw.strip().lower() in ("1", "true", "yes", "y", "t")
                return bool(raw)

            for abl in abl_list:
                abl_idx = abl["json_index"]
                ref_forward = [r for r in ref_indices if r >= abl_idx]
                if ref_forward:
                    chosen_ref_idx = min(ref_forward)
                else:
                    ref_backward = [r for r in ref_indices if r < abl_idx]
                    chosen_ref_idx = max(ref_backward) if ref_backward else None

                if chosen_ref_idx is not None:
                    ref_entry = next(ref for ref in ref_list if ref["json_index"] == chosen_ref_idx)
                else:
                    ref_entry = None

                abl_valid = parse_valid_flag(abl["data"])
                ref_valid = parse_valid_flag(ref_entry["data"]) if ref_entry else None

                bundles.append(
                    {
                        "Abl_01": {
                            "data": abl["data"],
                            "json_index": abl_idx,
                            "valid": abl_valid,
                        },
                        "Ref_01": {
                            "data": ref_entry["data"],
                            "json_index": ref_entry["json_index"],
                            "valid": ref_valid,
                        }
                        if ref_entry
                        else None,
                        "timestamp": abl["timestamp"],
                    }
                )

            bundles.sort(key=lambda b: b["Abl_01"]["json_index"])
            self.matrix_history = bundles

            vt: Dict[int, Dict[str, Optional[bool]]] = {}
            for idx, bundle in enumerate(self.matrix_history):
                vt[idx] = {
                    "Abl_01": bundle["Abl_01"]["valid"],
                    "Ref_01": bundle["Ref_01"]["valid"] if bundle["Ref_01"] else None,
                }
            self.pNode.validityTable = vt

            deltas: List[float] = []
            for k in range(1, min(len(self.matrix_history), 500)):
                t1 = self.matrix_history[k - 1]["timestamp"]
                t2 = self.matrix_history[k]["timestamp"]
                if t1 is not None and t2 is not None and t2 > t1:
                    deltas.append(t2 - t1)

            if deltas:
                median_delta = np.median(deltas)
                self.native_interval_ms = max(5, int(median_delta * 1000))
            else:
                self.native_interval_ms = 100

            total_frames = len(self.matrix_history)
            self.pNode.totalReplayFrames = total_frames

            self.ui.file_label.setText(os.path.basename(file_path))
            self.ui.slider.setMinimum(0)
            self.ui.slider.setMaximum(max(0, total_frames - 1))

            if hasattr(self.pNode, "replayerActive"):
                self.pNode.replayerActive = True

            self.ui.mode_button.blockSignals(True)
            self.ui.mode_button.setChecked(True)
            self.ui.mode_button.blockSignals(False)
            self.set_mode_replay(True)

            self.current_idx = 0
            self.jump_to_frame(0)
            self.update_timer_speed()

            logging.info(
                f"CatheterReplayer: Loaded {total_frames} bundles, native interval ? {self.native_interval_ms} ms"
            )

        except Exception as e:
            logging.error(f"CatheterReplayer: Failed to load file: {e}")
            import traceback

            logging.error(traceback.format_exc())
            if hasattr(self.pNode, "replayerActive"):
                self.pNode.replayerActive = False

    # ------------------------------------------------------------------
    # Frame jump
    # ------------------------------------------------------------------
    def jump_to_frame(self, idx: int) -> None:
        if not self.matrix_history:
            return

        if not getattr(self.pNode, "replayModeActive", False):
            return

        vt = getattr(self.pNode, "validityTable", None)
        if not isinstance(vt, dict):
            logging.warning("CatheterReplayer.jump_to_frame: validityTable is not a dict")
            return

        self.current_idx = int(np.clip(idx, 0, len(self.matrix_history) - 1))
        self.pNode.currentReplayFrame = self.current_idx

        validity = vt.get(self.current_idx)
        if not validity:
            logging.warning(f"CatheterReplayer.jump_to_frame: no validity entry for index {self.current_idx}")
            return

        bundle = self.matrix_history[self.current_idx]
        abl_entry = bundle.get("Abl_01")
        ref_entry = bundle.get("Ref_01")

        if abl_entry:
            self.last_line_abl = abl_entry["json_index"]
        if ref_entry:
            self.last_line_ref = ref_entry["json_index"]

        ts = bundle.get("timestamp", 0.0)
        elapsed = ts - self.start_timestamp if self.start_timestamp else 0.0
        minutes = int(elapsed // 60)
        seconds = int(elapsed % 60)
        millis = int((elapsed - int(elapsed)) * 1000)
        time_str = f"{minutes:02}:{seconds:02}.{millis:03}"

        if self.ui:
            abl_json = self.last_line_abl if self.last_line_abl is not None else "-"
            ref_json = self.last_line_ref if self.last_line_ref is not None else "-"
            self.ui.info_label.setText(
                f"Bundle {self.current_idx} / {len(self.matrix_history) - 1}  |  "
                f"Abl JSON {abl_json}  |  "
                f"Ref JSON {ref_json}  |  "
                f"Time {time_str}"
            )
            try:
                self.ui.slider.blockSignals(True)
                self.ui.slider.setValue(self.current_idx)
            finally:
                self.ui.slider.blockSignals(False)

        manager = self.sceneManager
        seen_keys = set()

        def _extract_matrix(data: Dict[str, Any]) -> Optional[List[List[float]]]:
            cand = (
                data.get("matrix")
                or data.get("transform")
                or data.get("matrix4x4")
                or data.get("Matrix")
                or data.get("Transform")
            )

            if cand is None:
                return None

            if isinstance(cand, list) and len(cand) == 4 and all(isinstance(r, list) and len(r) == 4 for r in cand):
                return cand

            if isinstance(cand, list) and len(cand) == 16:
                return [cand[0:4], cand[4:8], cand[8:12], cand[12:16]]

            if isinstance(cand, dict):
                rows = cand.get("rows") or cand.get("Rows") or cand.get("data")
                if isinstance(rows, list) and len(rows) == 4 and all(isinstance(r, list) and len(r) == 4 for r in rows):
                    return rows

            logging.warning(f"CatheterReplayer: Unrecognized matrix format in JSON entry: {type(cand)}")
            return None

        def update_catheter(key: str, entry: Optional[Dict[str, Any]]) -> None:
            if entry is None:
                return

            seen_keys.add(key)

            data = entry["data"]
            is_valid = validity.get(key, True)
            if is_valid is None:
                is_valid = True

            tf = self.replay_transforms.get(key)
            model = self.models.get(key)

            if model is None:
                self._resolve_models_from_scene()
                model = self.models.get(key)

            if tf is None or model is None:
                return

            mat_list = _extract_matrix(data)
            if mat_list is None:
                return

            m = vtk.vtkMatrix4x4()
            for r in range(4):
                for c in range(4):
                    m.SetElement(r, c, float(mat_list[r][c]))
            tf.SetMatrixTransformToParent(m)

            tf.SetAttribute("OpenIGTLink.valid", "true" if is_valid else "false")
            tf.SetAttribute("TargetModelID", model.GetID())

            if manager and hasattr(manager, "updateCatheterVisuals"):
                manager.updateCatheterVisuals(model, bool(is_valid))

            model.Modified()

        update_catheter("Abl_01", abl_entry)
        update_catheter("Ref_01", ref_entry)

        if manager and hasattr(manager, "updateCatheterVisuals"):
            for key in self.KEYS:
                if key not in seen_keys:
                    model = self.models.get(key)
                    if model:
                        manager.updateCatheterVisuals(model, False)

        self._fpsFrameCount += 1
        now_ms = self._elapsedTimer.elapsed() if self._elapsedTimer.isValid() else None
        if now_ms is not None:
            if self._fpsLastUpdateMs is None:
                self._fpsLastUpdateMs = now_ms
            else:
                delta_ms = now_ms - self._fpsLastUpdateMs
                if delta_ms >= 500:
                    self._currentFPS = (self._fpsFrameCount * 1000.0) / max(delta_ms, 1)
                    self._fpsFrameCount = 0
                    self._fpsLastUpdateMs = now_ms
                    if self.ui:
                        self.ui.fps_label.setText(f"FPS: {self._currentFPS:4.1f}")

        slicer.util.forceRenderAllViews()

    # ------------------------------------------------------------------
    # Playback control
    # ------------------------------------------------------------------
    def toggle_playback(self, direction: int) -> None:
        if not self.matrix_history:
            return

        self.play_direction = 1 if direction >= 0 else -1

        if not self.ui:
            return

        if direction > 0:
            is_checked = self.ui.btn_play_stop.isChecked()
            if is_checked:
                self.ui.btn_rev.setChecked(False)
                self.ui.btn_rev.setText("Play Rev")
                self.start_playback()
                self.ui.btn_play_stop.setText("Stop")
            else:
                self.stop_playback()
                self.ui.btn_play_stop.setText("Play Fwd")
        else:
            is_checked = self.ui.btn_rev.isChecked()
            if is_checked:
                self.ui.btn_play_stop.setChecked(False)
                self.ui.btn_play_stop.setText("Play Fwd")
                self.start_playback()
                self.ui.btn_rev.setText("Stop Rev")
            else:
                self.stop_playback()
                self.ui.btn_rev.setText("Play Rev")

    def start_playback(self) -> None:
        if not self.matrix_history:
            return

        self._elapsedTimer.start()
        self._replayStartWallMs = self._elapsedTimer.elapsed()
        self._fpsFrameCount = 0
        self._fpsLastUpdateMs = self._elapsedTimer.elapsed()
        self._currentFPS = 0.0

        self.update_timer_speed()
        self.timer.start()

    def stop_playback(self) -> None:
        if self.timer:
            self.timer.stop()

    # ------------------------------------------------------------------
    # Timer speed
    # ------------------------------------------------------------------
    def update_timer_speed(self) -> None:
        """
        Update internal speed factor based on UI combo and adjust timer interval.
        """
        if not self.ui:
            self._speed_factor = 1.0
            return

        text = self.ui.speed_combo.currentText  # property, not callable

        try:
            if text.endswith("x"):
                self._speed_factor = float(text[:-1])
            else:
                self._speed_factor = float(text)
        except Exception:
            self._speed_factor = 1.0

        if self._playbackMode == "frame":
            interval = max(5, int(self.native_interval_ms / max(self._speed_factor, 0.01)))
        else:
            interval = 10  # small tick; real-time mode uses wall-clock to pick frame

        if self.timer:
            self.timer.setInterval(interval)

    # ------------------------------------------------------------------
    # Auto-play loop
    # ------------------------------------------------------------------
    def process_auto_play(self) -> None:
        if not self.matrix_history:
            return

        if not getattr(self.pNode, "replayModeActive", False):
            self.stop_playback()
            return

        if self._playbackMode == "frame":
            next_idx = self.current_idx + self.play_direction
            if next_idx < 0 or next_idx >= len(self.matrix_history):
                self.stop_playback()
                if self.ui:
                    if self.play_direction > 0:
                        self.ui.btn_play_stop.setChecked(False)
                        self.ui.btn_play_stop.setText("Play Fwd")
                    else:
                        self.ui.btn_rev.setChecked(False)
                        self.ui.btn_rev.setText("Play Rev")
                return
            self.jump_to_frame(next_idx)
        else:
            if not self._elapsedTimer.isValid() or self._replayStartWallMs is None:
                self._elapsedTimer.start()
                self._replayStartWallMs = self._elapsedTimer.elapsed()

            now_ms = self._elapsedTimer.elapsed()
            elapsed_ms = now_ms - self._replayStartWallMs
            elapsed_s = elapsed_ms / 1000.0

            if self.start_timestamp is None:
                self.start_timestamp = self.matrix_history[0].get("timestamp", 0.0)

            target_time = self.start_timestamp + elapsed_s * max(self._speed_factor, 0.01)

            if self.play_direction < 0:
                target_time = (
                    self.start_timestamp
                    + max(0.0, (self.matrix_history[-1].get("timestamp", self.start_timestamp) - self.start_timestamp))
                    - elapsed_s * max(self._speed_factor, 0.01)
                )

            best_idx = self.current_idx
            best_dt = float("inf")
            for i, bundle in enumerate(self.matrix_history):
                ts = bundle.get("timestamp", None)
                if ts is None:
                    continue
                dt = abs(ts - target_time)
                if dt < best_dt:
                    best_dt = dt
                    best_idx = i

            if best_idx < 0 or best_idx >= len(self.matrix_history):
                self.stop_playback()
                if self.ui:
                    if self.play_direction > 0:
                        self.ui.btn_play_stop.setChecked(False)
                        self.ui.btn_play_stop.setText("Play Fwd")
                    else:
                        self.ui.btn_rev.setChecked(False)
                        self.ui.btn_rev.setText("Play Rev")
                return

            self.jump_to_frame(best_idx)

    # ------------------------------------------------------------------
    # Cleanup
    # ------------------------------------------------------------------
    def cleanup(self) -> None:
        """
        Cleanup replay-specific resources.
        Does NOT touch EPCMRLogic observers; only local timer/UI/state.
        """
        try:
            if self.timer:
                self.timer.stop()
        except Exception:
            pass
        self.timer = None

        try:
            if self.ui:
                self.ui.deleteLater()
        except Exception:
            pass
        self.ui = None

        if hasattr(self.pNode, "replayerActive"):
            self.pNode.replayerActive = False
        if hasattr(self.pNode, "replayModeActive"):
            self.pNode.replayModeActive = False

        logging.info("CatheterReplayer: cleanup completed.")

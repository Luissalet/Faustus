"""Read a video and write per-frame 3D body landmarks as JSON for landmarks_to_action.py.

  python extract_landmarks.py --video clip.mp4 --model pose_landmarker_lite.task --out pose.json [--every 1]

Needs `mediapipe` and `opencv-python` (CPU only, no GPU, no hosted API) and a pose landmarker model file
(`pose_landmarker_lite|full|heavy.task`, Apache-2.0, published by the MediaPipe project; download it once and keep
it outside the project repo). Frames where no body is found are skipped and counted, and the output keeps
the source frame number under `_source_frame` (landmarks_to_action.py ignores it and plays the kept frames in order). World landmarks are metres with the origin at the hips.
The first person only is read; a clip with several people needs cropping first.
"""
import argparse
import json
import sys

NAMES = ["NOSE", "LEFT_EYE_INNER", "LEFT_EYE", "LEFT_EYE_OUTER", "RIGHT_EYE_INNER", "RIGHT_EYE", "RIGHT_EYE_OUTER",
         "LEFT_EAR", "RIGHT_EAR", "MOUTH_LEFT", "MOUTH_RIGHT", "LEFT_SHOULDER", "RIGHT_SHOULDER", "LEFT_ELBOW",
         "RIGHT_ELBOW", "LEFT_WRIST", "RIGHT_WRIST", "LEFT_PINKY", "RIGHT_PINKY", "LEFT_INDEX", "RIGHT_INDEX",
         "LEFT_THUMB", "RIGHT_THUMB", "LEFT_HIP", "RIGHT_HIP", "LEFT_KNEE", "RIGHT_KNEE", "LEFT_ANKLE", "RIGHT_ANKLE",
         "LEFT_HEEL", "RIGHT_HEEL", "LEFT_FOOT_INDEX", "RIGHT_FOOT_INDEX"]


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--video", required=True)
    p.add_argument("--model", required=True)
    p.add_argument("--out", required=True)
    p.add_argument("--every", type=int, default=1, help="keep one frame in N")
    a = p.parse_args()
    import cv2
    import mediapipe as mp
    from mediapipe.tasks import python as tasks
    from mediapipe.tasks.python import vision

    options = vision.PoseLandmarkerOptions(base_options=tasks.BaseOptions(model_asset_path=a.model),
                                           running_mode=vision.RunningMode.VIDEO, num_poses=1)
    cap = cv2.VideoCapture(a.video)
    if not cap.isOpened():
        raise SystemExit("cannot open " + a.video)
    fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
    frames, missed, index = [], 0, -1
    with vision.PoseLandmarker.create_from_options(options) as landmarker:
        while True:
            ok, bgr = cap.read()
            if not ok:
                break
            index += 1
            if index % a.every:
                continue
            image = mp.Image(image_format=mp.ImageFormat.SRGB, data=cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB))
            result = landmarker.detect_for_video(image, int(index * 1000 / fps))
            if not result.pose_world_landmarks:
                missed += 1
                continue
            frame = {n: [l.x, l.y, l.z] for n, l in zip(NAMES, result.pose_world_landmarks[0])}
            frame["_source_frame"] = index
            frames.append(frame)
    json.dump({"fps": fps / a.every, "source": a.video, "missed_frames": missed, "frames": frames},
              open(a.out, "w", encoding="utf-8"))
    print("LANDMARKS %d frames, %d without a body, %.1f fps" % (len(frames), missed, fps / a.every))
    sys.exit(0 if frames else 1)


main()

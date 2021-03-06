import os
import cv2
import numpy as np
from pytube import YouTube
from tqdm import tqdm
import tempfile

def LKOpticalFlow(frame1, frame2):
    frame = frame1.copy()
    frame = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)

    # features that we can track between frames
    kp1 = cv2.goodFeaturesToTrack(
        frame,
        mask = None,
        maxCorners = 1000,
        qualityLevel = 0.3,
        minDistance = 7,
        blockSize = 7)

    nextFrame = frame2.copy()
    nextFrame = cv2.cvtColor(nextFrame, cv2.COLOR_BGR2GRAY)

    #using Lucas Kanade optical flow algorithm, find the same keypoints in the next frame. This can be done with
    # SIFT feature matching as well. Room for experimentation
    kp2, st, err = cv2.calcOpticalFlowPyrLK(
        frame,
        nextFrame,
        kp1,
        None,
        winSize  = (15, 15),
        maxLevel = 4,
        criteria = (cv2.TERM_CRITERIA_EPS | cv2.TERM_CRITERIA_COUNT, 20, 0.03))

    #print(kp1.shape, st.shape)
    return kp1[st==1], kp2[st==1]

# Using the keypoints in the old and new frame, get motion vectors
def getFeatureMotionVectors(kp, kpNew):
    disp = kpNew - kp
    return np.expand_dims(np.median(disp, 0), 0)


def motionVectorVisualization(KpO, KpN, frame):
  visualizedFrame = frame.copy()

  for i in range(KpO.shape[0]):
    ptO = np.floor(KpO[i]).astype(int)
    ptN = np.floor(KpN[i]).astype(int)

    visualizedFrame = cv2.line(visualizedFrame, tuple(ptO.tolist()), tuple(ptN.tolist()), (0, 255, 0), 3)

  return visualizedFrame

def warpSingleFrame(frame, disp):
    pt0 = np.asarray([
        [0, 0],
        [0, frame.shape[1] - 1],
        [frame.shape[0] - 1, 0],
        [frame.shape[0]-1, frame.shape[1] - 1]
    ])

    displacement = np.asarray([
        [disp[0], disp[1]]
    ])

    pt1 = pt0 + displacement

    M, mask = cv2.findHomography(pt0, pt1, cv2.RANSAC)
    warpedFrame = cv2.warpPerspective(frame , M, (frame.shape[1], frame.shape[0]))

    return warpedFrame


def optimizePath(c, iterations=100, window_size=6, lambda_t=5):
    t = c.shape[0]
    alpha = 0.0001
    #beta = 0.9

    # bilateral weights for the sum of differences
    W = np.asarray(
        [[r] for r in range(-window_size//2, window_size//2+1)]
    )
    W = np.exp((-9*(W)**2)/window_size**2)

    # sum of differences kernal for local patches of motion
    sumDiffKernal = -np.ones((window_size + 1, 1))
    sumDiffKernal[window_size//2 + 1] = window_size + 1

    p = c.copy()

    # Iteratively minimize objective function proposed in the MeshFlow paper
    # using gradient descent
    for iteration in range(iterations):
        # gradient for local sum of square differences used as a smoothing factor
        # minimizes big jumps in motion between frames
        diff = cv2.filter2D(p, -1, sumDiffKernal)
        smooth = cv2.filter2D(diff, -1, W)

        # gradient for the anchor term to keep the optimized motion close to the
        # original camera path to reduce cropping
        anchor = p - c

        p -= alpha*((anchor) + (lambda_t * smooth))

    return p

def bilateral(r, smoothingRadius):
    return np.exp((-9*(r)**2)/smoothingRadius**2)

def optimizeMotionCurve(c, iters=100, window=6, lambda_t=500):
    t_max = c.shape[0]

    # a bilatera kernal to give higher weight to frames closer to the current frame
    W = np.asarray(
        [[bilateral(r, window)] for r in range(-window//2, window//2+1)]
    )

    # iterative jacobi method for minimizing the quadratic formula used to
    # quantify smoothness of a video
    p = c.copy()
    g = 1 + (2 * lambda_t * np.expand_dims(W.sum(axis=0), 0))

    for i in range(iters):
        p = (c + (2 * lambda_t * cv2.filter2D(p, -1, W)))/g

    return p

class Stabilizer:
    def __init__(self, path, inName, outName, cropPercentage):
        self.inPath = os.path.join(path, inName)
        self.outPath = os.path.join(path, outName)
        self.cropFactor = cropPercentage / 100.0

    def cleanFiles(self):
        os.remove(self.inPath)
        os.remove(self.outPath)

    def generateStableVideo(self, updateMotion):
        cap = cv2.VideoCapture(self.inPath)

        if (cap.isOpened() == False):
            print("Error opening video stream or file")
            return

        fourecc = cv2.VideoWriter_fourcc(*'mp4v')

        # get the info the video needed for the video
        fps = cap.get(cv2.CAP_PROP_FPS)
        originalWidth = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
        originalHeight = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))

        cropWidthOffset = int((originalWidth * self.cropFactor) // 2)
        cropHeightOffset = int((originalHeight * self.cropFactor) // 2)

        size = (originalWidth - (2 * cropWidthOffset), \
                originalHeight - (2 * cropHeightOffset))

        #print(size, originalWidth, originalHeight, cropWidthOffset, cropHeightOffset, self.cropFactor)

        out = cv2.VideoWriter(self.outPath, fourecc, fps, size)

        ret, frame = cap.read()

        i = 0
        for m in tqdm(updateMotion):
            #newFrame = motionVectorVisualization(kps[i][0], kps[i][1], frame)
            newFrame = warpSingleFrame(frame, updateMotion[i])
            croppedFrame = newFrame[cropHeightOffset : originalHeight - cropHeightOffset,\
                                    cropWidthOffset : originalWidth - cropWidthOffset]

            #print(newFrame.shape, croppedFrame.shape, size, cropWidthOffset, cropHeightOffset)
            out.write(croppedFrame)

            ret, frame = cap.read()

            i += 1

        #cv2.destroyAllWindows()
        cap.release()
        out.release()


    def estimatedMotionPath(self):
        cap = cv2.VideoCapture(self.inPath)
        # Check if camera opened successfully
        if (cap.isOpened() == False):
            print("Error opening video stream or file")
            return

        motion = np.zeros((1, 2))
        ret, frameOld = cap.read()
        ret, frameNew = cap.read()

        while ret:
            try:
                kpOld, kpNew = LKOpticalFlow(frameOld, frameNew)

                if kpOld.shape[0] > 0:
                    motionVectors = getFeatureMotionVectors(kpOld, kpNew)
                    motion = np.append(motion, motion[[-1]] + motionVectors, 0)
                else:
                    motion = np.append(motion, motion[[-1]], 0)
            except:
                motion = np.append(motion, motion[[-1]], 0)

            frameOld = frameNew
            ret, frameNew = cap.read()

        cap.release()
        # Closes all the frames
        #cv2.destroyAllWindows()

        return motion


    def stabilizeVideo(self):
        motion = self.estimatedMotionPath()
        smoothMotion = optimizeMotionCurve(motion)
        smoothMotion2 = optimizePath(motion)
        updateMotion = smoothMotion - motion

        ## Just for visualizations
        #np.savetxt("output/motion.txt", motion)
        #np.savetxt("output/smoothMotion.txt", smoothMotion)
        #np.savetxt("output/smoothMotion2.txt", smoothMotion2)

        self.generateStableVideo(updateMotion)


def stabilize(videoFile, cropPercentage):
    inName = next(tempfile._get_candidate_names())
    outName = next(tempfile._get_candidate_names()) + '.mp4'
    path = "/tmp/"

    videoFile.save(os.path.join(path, inName))
    s = Stabilizer(path, inName, outName, int(cropPercentage))
    s.stabilizeVideo()

    out = open(s.outPath, "rb")

    s.cleanFiles()

    return out

def stabilizeYoutube(youtubeurl, cropPercentage):
    path = "/tmp/"
    inName = next(tempfile._get_candidate_names())
    inName = YouTube(youtubeurl).streams.first().download(output_path=path, filename=inName)
    outName = next(tempfile._get_candidate_names()) + '.mp4'

    s = Stabilizer(path, inName, outName, int(cropPercentage))
    s.stabilizeVideo()

    out = open(s.outPath, "rb")

    s.cleanFiles()

    return out

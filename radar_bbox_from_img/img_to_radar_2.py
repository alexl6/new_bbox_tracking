import re
import os

import numpy as np
import scipy.ndimage
import matplotlib.pyplot as plt
import matplotlib.patches as patches

from convert import calc_conversion_grid
from radar_utilities import load_yolo, load_transform_mat, polar_in_cartesian, img_to_radar_cartesian, swap, dim_ratio
from radar_velocity import get_centroids, get_velocities, find_uid_idx

from typing import List, Dict
from numpy.typing import NDArray

MAX_RADAR_RADIUS = 30
GRID_DIM_X = 280
GRID_DIM_Y = 140
GRID_RES = 0.2

def label_conv(label: int) -> List[float]:
    """
    Converts an object label into a tuple of estimated dimensions (x, y)

    :param label: The original label of the object
    :return: The estimated dimensions in (length, width). Both represented as floats
    """
    mapping = {0: [0.7, 0.7],
               1: [0.6, 1.7],
               2: [1.8, 4.5],
               3: [0.8, 2],
               5: [2.5, 12],
               7: [2.6, 15],
               80: [0.6, 1.7]
               }
    return mapping[label]


def get_radar_bg_intensity(f_num: int, seq_path):
    """
    Obtains the visual representation of a given radar frame

    :param f_num: frame number
    :param seq_path: path to the directory of radar detections
    :return: radar intensity in a flattened array
    """
    radar_dir = os.path.join(seq_path, "radar")

    frame_num = str(f_num).zfill(6) + ".npy"

    a = np.load(os.path.join(radar_dir, frame_num))
    b = a[:, :, 0] ** 2 + a[:, :, 1] ** 2

    return b.flatten()


def probe_frames_by_uid(uid: int, uid_map: List[map], curr: int, num: int, step:int = 1, backwards:bool = False):
    """
    Probe future or past frames to see if it contains a bounding box with the caller specified uid.
    Starts from the frame that's `num` frames away from the current frame until we find a match.

    :param uid: uid to search
    :param uid_map: List of maps containing a mapping from uid to the bounding box's index
    :param curr: current frame
    :param num: specifies the distance of the starting frame to the current frame
    :param step: size of the steps
    :param backwards: Probes past frames if True. Default is False (probe future frames).
    :return: The furthest frame (<=`num` frames away from the curr) containing a matching uid.
    """
    if backwards:
        for i in range(max(0, curr - num * step), curr, step):
            if uid in uid_map[i]:
                return i
    else:
        for i in range(min(len(uid_map)-1, curr + 1 + num * step), curr + 1, -step):
            if uid in uid_map[i]:
                return i

    return curr


def matched_filter_image(seq_path: str, centroids: List[NDArray], x, y, uid_map: List[Dict], eval_frame: int = 100, uid = '0'):
    """
    Runs match filter for all bounding boxes in a given frame
    :param seq_path:
    :param centroids:
    :param x:
    :param y:
    :param uid_map:
    :param eval_frame:
    :return:
    """
    print(eval_frame)
    # Check if we have the same number of radar files (background image) and trajectory centroid entries
    num_frames = 12
    step_size = 2
    num_radar_files: int = len(os.listdir(os.path.join(seq_path, "radar")))
    assert (num_radar_files == len(centroids))

    #TODO: Put this in a loop to run match filter for every bbox in the frame
    uid = '3'

    # Get the polar radar image
    z = get_radar_bg_intensity(eval_frame, seq_path)
    base_image = scale_to_grid(x, y, z)

    base_loc = centroids[eval_frame][uid_map[eval_frame][uid]][1:3]
    print("Base centroids location: %s"%base_loc)

    # Try to find future frames containing matching uid
    future_frames = range(eval_frame + step_size, probe_frames_by_uid(uid, uid_map, eval_frame, num_frames, step=step_size), step_size)
    # Find past frames with matching uid if there isn't enough future frames
    past_frames = range(probe_frames_by_uid(uid, uid_map, eval_frame, num_frames - len(future_frames), backwards=True, step=step_size), eval_frame, step_size)

    frames = list(past_frames) + list(future_frames)
    print("Frames used: %s"%frames)
    # Predetermine images multiply dimensions based on num of frames avaiable
    images_multiply = np.empty((len(frames) * GRID_DIM_X * GRID_DIM_Y)).reshape((len(frames), GRID_DIM_X, GRID_DIM_Y))
    assert(len(images_multiply.shape)==3)
    # accumulated_sum = np.zeros((GRID_DIM_X, GRID_DIM_Y))
    accumulated_sum2 = np.zeros((GRID_DIM_X, GRID_DIM_Y))
    img_to_multiply = []
    neighbor_img = base_image

    sum = base_image.copy()

    # Start with future frames
    for i, f in enumerate(frames):
        z = get_radar_bg_intensity(f, seq_path)
        image = scale_to_grid(x, y, z)
        sum += image
        obj_loc = centroids[f][uid_map[f][uid]][1:3]
        print("Obj location: %s"%obj_loc)
        loc_diff = (base_loc - obj_loc) / GRID_RES
        print("Scaled location diff: %s"%loc_diff)
        image = scipy.ndimage.shift(image, shift=loc_diff, mode='constant')

        # Fuse with neighbor_img & copy to img_to_multiply for every other img
        if i % 2 == 1:
            img_to_multiply.append(neighbor_img+image)
        else:
            neighbor_img = image

        # plt.imsave("%i.png"%i, image)
        # image_multiply = np.multiply(image, base_image)
        # accumulated_sum += image_multiply

        # accumulated_sum2 += np.multiply(image, img_to_multiply)
    for i in range(len(img_to_multiply)-1):
        accumulated_sum2 += np.multiply(img_to_multiply[i], img_to_multiply[i+1])
    plt.imshow(sum)
    plt.show()
    input()
    return accumulated_sum2.T



def scale_to_grid(x: NDArray, y: NDArray, z: NDArray):
    grid = np.zeros((GRID_DIM_X, GRID_DIM_Y))
    grid_count = np.ones((GRID_DIM_X, GRID_DIM_Y))
    for i in range(len(z)):
        if grid[x[i], y[i]] != 0:
            grid_count[x[i], y[i]] += 1
        grid[x[i], y[i]] += z[i]

    # Divide each virtual `pixel` by the number of physical `pixel` behind it
    return grid / grid_count


def map_to_grid(x: NDArray, y: NDArray, precision: float = 0.25):
    return x // precision, y // precision


def batch_process_to_img(img_bboxes, seq_path, centroids, centroids_v, radar_label_path, x, y):
    # Process every frame in the sequence
    for f_num in range(len(img_bboxes)):
        if f_num % (len(img_bboxes) // 4) == 0:
            print("%i%% Done" % (100 * (f_num / len(img_bboxes))))
        z = get_radar_bg_intensity(f_num, seq_path)
        # Clear previous drawings
        plt.clf()
        # Draw radar background
        plt.scatter(x, y, c=z)
        # Draw every box in this frame
        for i in range(img_bboxes[f_num].shape[0]):
            # Skip bboxes outside of radar range
            if centroids[f_num][i, 2] > MAX_RADAR_RADIUS:
                continue

            # Get the bounding box dimensions from object type label
            # Assume object is 'vertical' by default
            radar_bbox_dim: List[float] = label_conv(img_bboxes[f_num][i, 0])

            # first check the velocity, prefer determining orientation by velocity
            if np.sum(np.abs(centroids_v[f_num][i, 0:2] ** 2)) > 0.08:
                # If there's greater horizontal movement
                if abs(centroids_v[f_num][i, 0]) / abs(centroids_v[f_num][i, 0]) > 1.1:
                    swap(radar_bbox_dim)
            # Check box shape if the object is reasonably far away
            elif centroids[f_num][i, 2] > 8 or (abs(centroids[f_num][i, 1]) < 1 and centroids[f_num][i, 2] > 5):
                # Use the bounding box aspect ratio to determine object orientation
                if dim_ratio(img_bboxes[f_num][i, :]) > 2:
                    swap(radar_bbox_dim)

            plt.gca().add_patch(patches.Rectangle(
                (centroids[f_num][i, 1] - radar_bbox_dim[0] / 2, centroids[f_num][i, 2] - radar_bbox_dim[1] / 2),
                radar_bbox_dim[0], radar_bbox_dim[1], fill=True, color='pink', alpha=0.35,
                zorder=100))
        # Use the same scaling for x,y-axis, configure bounds, display
        plt.axis('square')
        plt.xlim((-MAX_RADAR_RADIUS, MAX_RADAR_RADIUS))
        plt.ylim((0, MAX_RADAR_RADIUS))
        # plt.scatter(radar_posn[:,1], radar_posn[:, 2], c='red', alpha = 0.8)

        fig_name = str(f_num).zfill(10) + '.png'
        plt.savefig(os.path.join(radar_label_path, fig_name))


def batch_process_interactive(img_bboxes, seq_path, centroids, centroids_v, x, y, uid_mapping):
    # Enable interactive mode
    plt.ion()
    while True:
        # Take user input/exit gracefully
        try:
            f_num = int(input("Frame num?\t"))
        except EOFError:
            print("Exiting...")
            exit()

        # Retry if frame number out of range
        if f_num < 0 or f_num >= len(img_bboxes):
            continue

        # # Get radar background
        # z = get_radar_bg_intensity(f_num, seq_path)

        # Clear previous drawings
        plt.clf()
        # # Draw radar background (Raw)
        # plt.scatter(x, y, c=z)
        # # Draw radar background (Scaled)
        # grid = scale_to_grid(x, y, z)
        # plt.imshow(grid.T)
        uids = list(uid_mapping[f_num].keys())
        uid = '3'
        print("Match filter on type %s"%img_bboxes[f_num][uid_mapping[f_num][uid], 0])
        #TODO: Experimental
        plt.imshow(matched_filter_image(seq_path, centroids, x, y, uid_mapping, f_num, uid), vmax=0.7)
        plt.ylim((0, GRID_DIM_Y))

        # Draw every box in this frame
        for i in range(img_bboxes[f_num].shape[0]):
            # Skip bboxes outside of radar range
            if centroids[f_num][i, 2] > MAX_RADAR_RADIUS:
                continue

            # Get the bounding box dimensions from object type label
            # Assume object is 'vertical' by default
            radar_bbox_dim: List[float] = label_conv(img_bboxes[f_num][i, 0])

            # first check the velocity, prefer determining orientation by velocity
            if np.sum(np.abs(centroids_v[f_num][i, 0:2] ** 2)) > 0.08:
                # If there's greater horizontal movement
                if abs(centroids_v[f_num][i, 0]) / abs(centroids_v[f_num][i, 0]) > 1.1:
                    swap(radar_bbox_dim)
            # Check box shape if the object is reasonably far away
            elif centroids[f_num][i, 2] > 8 or (abs(centroids[f_num][i, 1]) < 1 and centroids[f_num][i, 2] > 5):
                # Use the bounding box aspect ratio to determine object orientation
                if dim_ratio(img_bboxes[f_num][i, :]) > 2:
                    swap(radar_bbox_dim)

            # plt.gca().add_patch(patches.Rectangle(
            #     (centroids[f_num][i, 1] - radar_bbox_dim[0] / 2, centroids[f_num][i, 2] - radar_bbox_dim[1] / 2),
            #     radar_bbox_dim[0], radar_bbox_dim[1], fill=True, color='pink', alpha=0.35,
            #     zorder=100))
            # print("%i, %i" % (centroids[f_num][i, 1], centroids[f_num][i, 2]))
            # print("%i, %i"%(centroids[f_num][i, 1]/GRID_RES + GRID_DIM_X/2, centroids[f_num][i, 2]/GRID_RES))

            plt.gca().add_patch(patches.Rectangle(
                ((centroids[f_num][i, 1] - radar_bbox_dim[0] / 2)/GRID_RES + GRID_DIM_X/2, (centroids[f_num][i, 2] - radar_bbox_dim[1] / 2)/GRID_RES),
                radar_bbox_dim[0]/GRID_RES, radar_bbox_dim[1]/GRID_RES, fill=True, color='pink', alpha=0.25,
                zorder=100))
        # Use the same scaling for x,y-axis, configure bounds, display
        # plt.axis('square')
        # plt.xlim((-MAX_RADAR_RADIUS, MAX_RADAR_RADIUS))
        # plt.ylim((0, MAX_RADAR_RADIUS))
        # plt.scatter(radar_posn[:,1], radar_posn[:, 2], c='red', alpha = 0.8)
        plt.show()



def batch_process_interactive2(img_bboxes, seq_path, centroids, centroids_v, x, y):
    # Enable interactive mode
    plt.ion()
    while True:
        # Take user input/exit gracefully
        try:
            f_num = int(input("Frame num?\t"))
        except EOFError:
            print("Exiting...")
            exit()
        # Retry if frame number out of range
        if f_num < 0 or f_num >= len(img_bboxes):
            continue
        # Get radar background
        z = get_radar_bg_intensity(f_num, seq_path)
        # Clear previous drawings
        plt.clf()
        # Draw radar background
        plt.scatter(x, y, c=z)
        # Draw every box in this frame
        for i in range(img_bboxes[f_num].shape[0]):
            # Skip bboxes outside of radar range
            if centroids[f_num][i, 2] > MAX_RADAR_RADIUS:
                continue
            # Get the bounding box dimensions from object type label
            # Assume object is 'vertical' by default
            radar_bbox_dim: List[float] = label_conv(img_bboxes[f_num][i, 0])
            # first check the velocity, prefer determining orientation by velocity
            if np.sum(np.abs(centroids_v[f_num][i, 0:2] ** 2)) > 0.08:
                # If there's greater horizontal movement
                if abs(centroids_v[f_num][i, 0]) / abs(centroids_v[f_num][i, 0]) > 1.1:
                    swap(radar_bbox_dim)
            # Check box shape if the object is reasonably far away
            elif centroids[f_num][i, 2] > 8 or (abs(centroids[f_num][i, 1]) < 1 and centroids[f_num][i, 2] > 5):
                # Use the bounding box aspect ratio to determine object orientation
                if dim_ratio(img_bboxes[f_num][i, :]) > 2:
                    swap(radar_bbox_dim)
            print("%i, %i"%(centroids[f_num][i, 1], centroids[f_num][i, 2]))
            plt.gca().add_patch(patches.Rectangle(
                (centroids[f_num][i, 1] - radar_bbox_dim[0] / 2, centroids[f_num][i, 2] - radar_bbox_dim[1] / 2),
                radar_bbox_dim[0], radar_bbox_dim[1], fill=True, color='pink', alpha=0.35,
                zorder=100))
        # Use the same scaling for x,y-axis, configure bounds, display
        plt.axis('square')
        plt.xlim((-MAX_RADAR_RADIUS, MAX_RADAR_RADIUS))
        plt.ylim((0, MAX_RADAR_RADIUS))
        # plt.scatter(radar_posn[:,1], radar_posn[:, 2], c='red', alpha = 0.8)
        plt.show()



def batch_process_interactive3(img_bboxes, seq_path, centroids, centroids_v, x, y):
    # Enable interactive mode
    plt.ion()
    while True:
        # Take user input/exit gracefully
        try:
            f_num = int(input("Frame num?\t"))
        except EOFError:
            print("Exiting...")
            exit()

        # Retry if frame number out of range
        if f_num < 0 or f_num >= len(img_bboxes):
            continue

        # Get radar background
        z = get_radar_bg_intensity(f_num, seq_path)
        image = scale_to_grid(x, y, z)
        # Clear previous drawings
        plt.clf()
        plt.imshow(image.T, vmax=0.7)
        plt.ylim((0, GRID_DIM_Y))

        # Draw every box in this frame
        for i in range(img_bboxes[f_num].shape[0]):
            # Skip bboxes outside of radar range
            if centroids[f_num][i, 2] > MAX_RADAR_RADIUS:
                continue

            # Get the bounding box dimensions from object type label
            # Assume object is 'vertical' by default
            radar_bbox_dim: List[float] = label_conv(img_bboxes[f_num][i, 0])

            # first check the velocity, prefer determining orientation by velocity
            if np.sum(np.abs(centroids_v[f_num][i, 0:2] ** 2)) > 0.08:
                # If there's greater horizontal movement
                if abs(centroids_v[f_num][i, 0]) / abs(centroids_v[f_num][i, 0]) > 1.1:
                    swap(radar_bbox_dim)
            # Check box shape if the object is reasonably far away
            elif centroids[f_num][i, 2] > 8 or (abs(centroids[f_num][i, 1]) < 1 and centroids[f_num][i, 2] > 5):
                # Use the bounding box aspect ratio to determine object orientation
                if dim_ratio(img_bboxes[f_num][i, :]) > 2:
                    swap(radar_bbox_dim)

            # plt.gca().add_patch(patches.Rectangle(
            #     (centroids[f_num][i, 1] - radar_bbox_dim[0] / 2, centroids[f_num][i, 2] - radar_bbox_dim[1] / 2),
            #     radar_bbox_dim[0], radar_bbox_dim[1], fill=True, color='pink', alpha=0.35,
            #     zorder=100))
            # print("%i, %i" % (centroids[f_num][i, 1], centroids[f_num][i, 2]))
            # print("%i, %i"%(centroids[f_num][i, 1]/GRID_RES + GRID_DIM_X/2, centroids[f_num][i, 2]/GRID_RES))

            plt.gca().add_patch(patches.Rectangle(
                ((centroids[f_num][i, 1] - radar_bbox_dim[0] / 2)/GRID_RES + GRID_DIM_X/2, (centroids[f_num][i, 2] - radar_bbox_dim[1] / 2)/GRID_RES),
                radar_bbox_dim[0]/GRID_RES, radar_bbox_dim[1]/GRID_RES, fill=True, color='pink', alpha=0.25,
                zorder=100))
        # Use the same scaling for x,y-axis, configure bounds, display
        # plt.axis('square')
        # plt.xlim((-MAX_RADAR_RADIUS, MAX_RADAR_RADIUS))
        # plt.ylim((0, MAX_RADAR_RADIUS))
        # plt.scatter(radar_posn[:,1], radar_posn[:, 2], c='red', alpha = 0.8)
        plt.show()


if __name__ == '__main__':
    # Build path to image & radar data files
    src_path = "D:\\UWCR Data\\"
    seq_name = input("Seq name?\t")

    if seq_name == '':
        seq_name = "2019_04_30_mlms001"

    date_pattern = re.compile("\d{4}_\d{2}_\d{2}")
    date = date_pattern.search(seq_name).group()
    seq_path = os.path.join(src_path, date, seq_name)

    if not os.path.exists(src_path):
        print("Path does not exist...")
        exit(1)

    # Load YOLO detection for images
    print("Loading img bboxes")
    img_bboxes, uid_mapping = load_yolo(os.path.join(seq_path, "images_0", "YOLO"))

    # Load camera ==> radar transformation matrix
    trans_mat: NDArray = load_transform_mat("transform.mat")
    # Calculate polar to cartesian conversion
    range_grid, angle_grid = calc_conversion_grid()
    x, y = polar_in_cartesian(range_grid, angle_grid)


    print("Calculating radar positions & velocities")
    # Calculate image bbox centroids in cartesian radar plane and their velocities
    centroids = get_centroids(img_bboxes, trans_mat)
    centroids_v = get_velocities(centroids, uid_mapping)

    radar_label_path = os.path.join(seq_path, 'radar_label')
    if not os.path.exists(radar_label_path):
        os.makedirs(radar_label_path)

    # Map x,y to a grid
    scaled_x, scaled_y = map_to_grid(x, y, GRID_RES)
    scaled_x += int(GRID_DIM_X/2)
    scaled_x = scaled_x.astype(int)
    scaled_y = scaled_y.astype(int)

    if input("User matched filter? ") == '0':
        batch_process_interactive3(img_bboxes, seq_path, centroids, centroids_v, scaled_x, scaled_y)
    else:
        batch_process_interactive(img_bboxes, seq_path, centroids, centroids_v, scaled_x, scaled_y, uid_mapping)


    # batch_process_to_img(img_bboxes, seq_path, centroids, centroids_v, radar_label_path, x, y)
    plt.ion()
    while True:
        f_num = int(input("F num?"))
        matched_filter_image(seq_path, centroids, scaled_x, scaled_y, uid_mapping, f_num)


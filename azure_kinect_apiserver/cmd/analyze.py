import argparse
import concurrent.futures
import functools
import logging
import os
import pickle
from os import path as osp
from typing import Dict, Optional, List, Tuple

import cv2
import numpy as np
import tqdm

from azure_kinect_apiserver.common import (
    AzureKinectDataset,
    KinectSystemCfg,
    MulticalCameraInfo,
    PointCloudHelper,
    save_pcds,
)
from azure_kinect_apiserver.decoder import (
    ArucoDetectHelper
)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("azure_kinect_apiserver.cmd.analyze")


def analyze_worker_s1(opt: KinectSystemCfg, dataset: AzureKinectDataset, marker_length: float, marker_type: int) -> Tuple[
    Optional[List[Tuple[int, Dict[str, Tuple[np.ndarray, np.ndarray, bool]]]]], Optional[Exception]]:
    """
    Stage 1: Find all aruco markers in the dataset
    :param opt:
    :param dataset:
    :param marker_length:
    :param marker_type:
    :return:
    """

    num_frames = len(dataset)
    aruco_ctx = {
        cam_name: ArucoDetectHelper(marker_length=marker_length,
                                    aruco_type=marker_type,
                                    camera_distort=dataset.multical_calibration.get_distort(cam_name),
                                    camera_matrix=dataset.multical_calibration.get_intrinsic(cam_name)) for cam_name in dataset.camera_name_list
    }
    assert num_frames > 0, f"num_frames={num_frames}"

    start_time = dataset.get_system_action_start_timestamp()
    timestamp_offset = dataset.get_system_timestamp_offset()

    def _job(frame_idx, frame_path_pack: Dict[str, str]):
        curr_aruco_result = {}
        for cam_name in dataset.camera_name_list:
            color_img_path, depth_img_path = frame_path_pack[cam_name]
            color_frame = cv2.imread(color_img_path)
            depth_frame = cv2.imread(depth_img_path, cv2.IMREAD_ANYDEPTH)

            res, processed_color_frame, processed_depth_frame, err = aruco_ctx[cam_name].process_one_frame(color_frame, depth_frame, undistort=True, debug=opt.debug)
            if err is None:
                curr_aruco_result[cam_name] = res
                if opt.debug:
                    aruco_ctx[cam_name].vis_2d(res, processed_color_frame)
                    aruco_ctx[cam_name].vis_3d(res, processed_color_frame, processed_depth_frame)
        return frame_idx, curr_aruco_result

    detection_result = []

    with tqdm.tqdm(total=num_frames, desc="analyzing aruco tags") as pbar:
        for frame_idx in range(num_frames):
            frame_meta_pack, frame_path_pack = dataset[frame_idx]
            if frame_meta_pack[dataset.master_camera_name]['color_dev_ts_usec'] * 1e-6 + timestamp_offset < start_time:
                pbar.update()
                continue
            else:
                detection_result.append(_job(frame_idx, frame_path_pack))
                pbar.update()

    return detection_result, None


def translate_aruco_6dof_to_realworld(translation: np.ndarray, rvec: np.ndarray, tvec: np.ndarray) -> np.ndarray:
    res = np.eye(4)
    res[:3, :3] = cv2.Rodrigues(rvec)[0]
    res[:3, 3:4] = tvec
    return translation @ res


def analyze_worker_s2_merge(marker_detection: Dict[str, Dict[str, Tuple[np.ndarray, np.ndarray, bool]]],
                            dataset: AzureKinectDataset) -> Optional[List[np.ndarray]]:
    if marker_detection is None or len(marker_detection) == 0:
        return None

    if len(marker_detection) == 1:
        serial = list(marker_detection.keys())[0]
        trans_mat = dataset.multical_calibration.get_realworld_transmat() @ dataset.multical_calibration.get_extrinsic(serial)
        rvec, tvec, status = list(marker_detection.values())[0]
        if status:
            return [translate_aruco_6dof_to_realworld(trans_mat, rvec, tvec)]
        else:
            return [translate_aruco_6dof_to_realworld(trans_mat, rvec[0], tvec[0]),
                    translate_aruco_6dof_to_realworld(trans_mat, rvec[1], tvec[1])]

    else:
        candidates = []
        for cam_name in marker_detection.keys():
            trans_mat = dataset.multical_calibration.get_realworld_transmat() @ dataset.multical_calibration.get_extrinsic(cam_name)
            rvec, tvec, status = marker_detection[cam_name]
            if status and cam_name == dataset.master_camera_name:
                candidates = [translate_aruco_6dof_to_realworld(trans_mat, rvec, tvec)]
                break
            elif status:
                candidates.append(translate_aruco_6dof_to_realworld(trans_mat, rvec, tvec))
            else:
                candidates.append(translate_aruco_6dof_to_realworld(trans_mat, rvec[0], tvec[0]))
                candidates.append(translate_aruco_6dof_to_realworld(trans_mat, rvec[1], tvec[1]))
        return candidates


def analyze_worker_s2(opt: KinectSystemCfg,
                      dataset: AzureKinectDataset,
                      detection_result: List[Tuple[int, Dict[str, Tuple[np.ndarray, np.ndarray, bool]]]],
                      valid_id_list: List[str] = None) -> Tuple[Optional[Dict[str, np.ndarray]], Optional[Exception]]:
    """
    Aruco position fusion

    TODO: add more fusion methods, e.g. point cloud registration, using Kalman filter, etc.
    :param opt:
    :param dataset:
    :param detection_result:
    :param valid_id_list:
    :return:
    """
    detection_result_np_collection = {}

    for idx, result in enumerate(detection_result):
        frame_idx, aruco_result = result
        aruco_result: dict
        if len(aruco_result) == 0:
            continue

        curr_frame_result = {}
        for cam_name, result_per_cam in aruco_result.items():
            for marker_id in result_per_cam.keys():
                if marker_id not in curr_frame_result.keys():
                    curr_frame_result[marker_id] = {cam_name: aruco_result[cam_name][marker_id]}
                else:
                    curr_frame_result[marker_id][cam_name] = aruco_result[cam_name][marker_id]

        if len(curr_frame_result) == 0:
            continue
        else:
            for marker_id in curr_frame_result.keys():
                if marker_id not in detection_result_np_collection.keys():
                    detection_result_np_collection[marker_id] = np.empty((len(detection_result),), dtype=object)
                else:
                    detection_result_np_collection[marker_id][frame_idx] = analyze_worker_s2_merge(
                        curr_frame_result[marker_id],
                        dataset,
                    )

    detection_result_np_collection = {k: v for k, v in detection_result_np_collection.items() if k in valid_id_list}
    print(detection_result_np_collection)

    return detection_result_np_collection, None


def merge_multicam_pc(
        cam_info: MulticalCameraInfo,
        enable_finetune: bool,
        finetune_transform: Dict[str, np.ndarray],
        frame_path_pack: Dict[str, Tuple[str, str]],
        xlim: Tuple[float, float],
        ylim: Tuple[float, float],
        zlim: Tuple[float, float]):
    final_pc = {}
    for cam_name, (color_img_path, depth_img_path) in frame_path_pack.items():
        color_img, depth_img = cv2.imread(color_img_path), cv2.imread(depth_img_path, cv2.IMREAD_ANYDEPTH)
        cam_matrix = cam_info.get_intrinsic(cam_name)
        cam_dist = cam_info.get_distort(cam_name)

        # undistort
        color_undistort = cv2.undistort(color_img, cam_matrix, cam_dist)
        depth_undistort = cv2.undistort(depth_img, cam_matrix, cam_dist)

        # build point cloud
        raw_pc = PointCloudHelper(
            color_undistort,
            depth_undistort,
            camera_intrinsic_desc=(
                cam_info.get_resolution(cam_name)[0],
                cam_info.get_resolution(cam_name)[1],
                cam_info.get_intrinsic(cam_name)
            ),
            transform=cam_info.get_extrinsic(cam_name),
            enable_norm_filter=False,
            enable_denoise=True,
            denoise_radius=0.02,
            denoise_std_ratio=0.05
        )

        cropped_pc = raw_pc.pcd
        if enable_finetune and cam_name in finetune_transform.keys():
            cropped_pc.transform(finetune_transform[cam_name])

        # convert to realworld
        raw_pc.pcd.transform(cam_info.get_realworld_transmat())

        # crop by limits
        cropped_pc = PointCloudHelper.crop_by_xyz_limits(raw_pc.pcd, xlim, ylim, zlim)
        cropped_pc = PointCloudHelper.crop_by_hsv_limits_reverse(cropped_pc, [35, 77], [43, 255], [46, 255])

        final_pc[cam_name] = cropped_pc
    return final_pc


def analyze_worker_s3(opt: KinectSystemCfg,
                      dataset: AzureKinectDataset,
                      detection_result: Dict[str, np.ndarray],
                      margin: int = 10,
                      enable_finetune: bool = True,
                      quiet: bool = False):
    num_frames = len(dataset)
    assert num_frames > 0, f"num_frames={num_frames}"

    start_time = dataset.get_system_action_start_timestamp()
    timestamp_offset = dataset.get_system_timestamp_offset()
    cam_info = dataset.multical_calibration
    xlim, ylim, zlim = cam_info.get_workspace_limits(output_type=tuple)
    zlim[0] += 0.005  # raise the lower bound a little bit to avoid the floor

    # init variables
    finetune_transform = {}
    detection_map = functools.reduce(np.logical_or, [np.vectorize(lambda x: x is not None)(detection_result[marker_id]) for marker_id in detection_result.keys()])
    start_index = max(0, np.where(detection_map == True)[0].min() - margin)
    stop_index = min(len(detection_map), np.where(detection_map == True)[0].max() + margin)

    pcd_save_path = osp.join(dataset.kinect_path, "pcd_s3")
    if not osp.exists(pcd_save_path):
        os.makedirs(pcd_save_path)

    # if enable_finetune:
    #     logger.info("finetune enabled")
    #     for frame_idx in range(num_frames):
    #         frame_meta_pack, frame_path_pack = dataset[frame_idx]
    #         final_pc = merge_multicam_pc(cam_info, enable_finetune, finetune_transform, frame_path_pack, xlim, ylim, zlim)
    #         for cam_name in final_pc.keys():
    #             _, ind = final_pc[cam_name].remove_radius_outlier(nb_points=16, radius=0.02)
    #             final_pc[cam_name] = final_pc[cam_name].select_by_index(ind)
    #             _, ind = final_pc[cam_name].remove_statistical_outlier(nb_neighbors=20, std_ratio=2.0)
    #             final_pc[cam_name] = final_pc[cam_name].select_by_index(ind)
    #
    #         for cam_name in filter(lambda x: x != dataset.master_camera_name, final_pc.keys()):
    #             finetune_transform[cam_name] = colored_point_cloud_registration_fine(final_pc[cam_name], final_pc[dataset.master_camera_name])
    #             final_pc[cam_name].transform(finetune_transform[cam_name])
    #         finetune_transform[dataset.master_camera_name] = np.eye(4)
    #         logger.info("finetune transform:")
    #         logger.info(finetune_transform)
    #
    #         if not quiet:
    #             vis_pcds(final_pc.values(), fake_color=True)
    #             vis_pcds(final_pc.values())
    #             select_finetune = py_cli_interaction.must_parse_cli_bool("proceed with this finetune?", default_value=True)
    #             if select_finetune:
    #                 break
    #             else:
    #                 abort = py_cli_interaction.must_parse_cli_bool("abort?", default_value=False)
    #                 if abort:
    #                     enable_finetune = False
    #                     break
    #         else:
    #             break
    # if not ((enable_finetune and len(finetune_transform) > 0) or ((not enable_finetune) and len(finetune_transform) == 0)):
    #     logger.error("finetune_transform is not empty but finetune is disabled")
    #     return Exception("finetune_transform is not empty but finetune is disabled")

    finetune_transform = {cam_name: cam_info.get_icp_extrinsic_refinement(cam_name) for cam_name in dataset.camera_name_list}

    tpool = concurrent.futures.ThreadPoolExecutor(max_workers=16)
    with tqdm.tqdm(total=num_frames, desc="build point cloud") as pbar:
        for frame_idx in range(num_frames):
            frame_meta_pack, frame_path_pack = dataset[frame_idx]
            if any(
                    [
                        frame_meta_pack[dataset.master_camera_name]['color_dev_ts_usec'] * 1e-6 + timestamp_offset < start_time,
                        frame_idx < start_index,
                        frame_idx >= stop_index
                    ]
            ) and False:
                pbar.update(1)
                continue
            else:
                # Fuse point cloud
                final_pc = merge_multicam_pc(cam_info,
                                             enable_finetune,
                                             finetune_transform,
                                             frame_path_pack,
                                             xlim, ylim, zlim)

                tpool.submit(save_pcds, list(final_pc.values()), pcd_save_path, '%06d' % frame_idx, True)
                # save_pcds(list(final_pc.values()), pcd_save_path, '%06d' % frame_idx)
                pbar.update()
    tpool.shutdown(wait=True)

    # with open(osp.join(pcd_save_path, "detection_result_s3.pkl"), "wb") as f:
    #     pickle.dump(detection_result, f)

    return None


def main(args: argparse.Namespace):
    opt = KinectSystemCfg(args.config)
    logger.info("processing directory: {}".format(osp.realpath(args.data_dir)))

    kinect_dir = osp.join(args.data_dir, "kinect")

    valid_ids = args.valid_ids.split(",") if args.valid_ids is not None else None
    assert args.marker_type is not None, "please specify aruco type"
    assert args.marker_length is not None, "please specify marker length"
    aruco_type = getattr(cv2.aruco, args.marker_type)

    if not osp.exists(kinect_dir):
        logging.error("directory does not exist: {}".format(kinect_dir))
        return 1

    if not osp.exists(osp.join(kinect_dir, "calibration.json")):
        logging.error(f"calibration.json does not exist: {kinect_dir}, please copy it to {kinect_dir} and try again")
        return 1
    dataset = AzureKinectDataset(kinect_dir)
    dataset.load()

    # Stage 1
    logger.info("===== Stage 1 =====")
    detection_result_s1: Optional[List[Tuple[int, Dict[str, Tuple[np.ndarray, np.ndarray, bool]]]]] = None
    if not osp.exists(osp.join(kinect_dir, "detection_result_s1.pkl")):
        detection_result_s1, err = analyze_worker_s1(opt, dataset, args.marker_length, aruco_type)
        if err is not None:
            logger.error(err)
            return 1
        with open(osp.join(kinect_dir, "detection_result_s1.pkl"), "wb") as f:
            pickle.dump(detection_result_s1, f)
    if detection_result_s1 is None:
        with open(osp.join(kinect_dir, "detection_result_s1.pkl"), "rb") as f:
            detection_result_s1 = pickle.load(f)

    # Stage 2
    logger.info("===== Stage 2 =====")
    detection_result_s2: Dict[str, np.ndarray] = None
    if not osp.exists(osp.join(kinect_dir, "detection_result_s2.pkl")):
        detection_result_s2, err = analyze_worker_s2(opt, dataset, detection_result_s1, valid_ids)
        if err is not None:
            logger.error(err)
            return 1

        with open(osp.join(kinect_dir, "detection_result_s2.pkl"), "wb") as f:
            pickle.dump(detection_result_s2, f)
    if detection_result_s2 is None:
        with open(osp.join(kinect_dir, "detection_result_s2.pkl"), "rb") as f:
            detection_result_s2 = pickle.load(f)

    # Stage 3
    logger.info("===== Stage 3 =====")
    analyze_worker_s3(opt, dataset, detection_result_s2, enable_finetune=args.enable_finetune, quiet=args.quiet)
    return 0


def entry_point(argv):
    if len(argv) < 1:
        print("Usage: python -m azure_kinect_apiserver analyze <path>")
        return 1
    else:
        data_dir = argv[0]
        parser = argparse.ArgumentParser()
        parser.add_argument('--config', type=str, default='./azure_kinect_config.yaml')
        parser.add_argument('--marker_length', type=float, required=True, help='marker length in meter')
        parser.add_argument(
            '--marker_type',
            type=str,
            required=True,
            choices=[
                'DICT_4X4_100',
                'DICT_4X4_1000',
                'DICT_4X4_250',
                'DICT_4X4_50',
                'DICT_5X5_100',
                'DICT_5X5_1000',
                'DICT_5X5_250',
                'DICT_5X5_50',
                'DICT_6X6_100',
                'DICT_6X6_1000',
                'DICT_6X6_250',
                'DICT_6X6_50',
                'DICT_7X7_100',
                'DICT_7X7_1000',
                'DICT_7X7_250',
                'DICT_7X7_50',
            ],
            help='aruco dictionary type'
        )
        parser.add_argument('--valid_ids', type=str, default=None, help='valid marker ids')
        parser.add_argument('--quiet', action='store_true', help='disable interactive mode')
        parser.add_argument('--enable_finetune', action='store_true', help='enable finetune')

        args = parser.parse_args(argv[1:])
        args.data_dir = data_dir

        return main(args)


if __name__ == '__main__':
    import sys

    exit(entry_point(sys.argv[1:]))

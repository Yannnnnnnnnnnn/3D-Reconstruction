import argparse
import os
import torch
import torch.nn as nn
import torch.nn.parallel
import torch.backends.cudnn as cudnn
from torch.utils.data import DataLoader
import time
from datasets import find_dataset_def
from models import *
from utils import *
from datasets.data_io import read_pfm, save_pfm
import ast

# from datasets.data_io import read_cam_file, read_pair_file, read_image, read_map, save_image, save_map
import cv2
from plyfile import PlyData, PlyElement
from PIL import Image
import math


cudnn.benchmark = True

parser = argparse.ArgumentParser(description='Predict depth')

parser.add_argument('--inverse_depth', help='True or False flag, input should be either "True" or "False".',
                    type=ast.literal_eval, default=False)

parser.add_argument('--return_depth', help='True or False flag, input should be either "True" or "False".',
                    type=ast.literal_eval, default=True)

parser.add_argument('--max_h', type=int, default=512, help='Maximum image height when training')
parser.add_argument('--max_w', type=int, default=960, help='Maximum image width when training.')
parser.add_argument('--image_scale', type=float, default=1.0, help='pred depth map scale (compared to input image)')

parser.add_argument('--light_idx', type=int, default=3, help='select while in test')
parser.add_argument('--view_num', type=int, default=7, help='training view num setting')

parser.add_argument('--dataset', default='data_eval_transform', help='select dataset')
parser.add_argument('--testpath', help='testing data path')
parser.add_argument('--testlist', help='testing scan list')

parser.add_argument('--batch_size', type=int, default=1, help='testing batch size')
parser.add_argument('--numdepth', type=int, default=256, help='the number of depth values')
parser.add_argument('--interval_scale', type=float, default=0.8, help='the depth interval scale')

parser.add_argument('--loadckpt', default=None, help='load a specific checkpoint')
parser.add_argument('--outdir', default='./outputs', help='output dir')

# parse arguments and check
args = parser.parse_args()
print_args(args)

# TODO: check
# model_name = str.split(args.loadckpt, '/')[-2] + '_' + str.split(args.loadckpt, '/')[-1]
# save_dir = os.path.join(args.outdir, model_name)
# if not os.path.exists(save_dir):
#     print('save dir', save_dir)
#     os.makedirs(save_dir)
save_dir = args.outdir


# read intrinsics and extrinsics
def read_camera_parameters(filename):
    with open(filename) as f:
        lines = f.readlines()
        lines = [line.rstrip() for line in lines]
    # extrinsics: line [1,5), 4x4 matrix
    extrinsics = np.fromstring(' '.join(lines[1:5]), dtype=np.float32, sep=' ').reshape((4, 4))
    # intrinsics: line [7-10), 3x3 matrix
    intrinsics = np.fromstring(' '.join(lines[7:10]), dtype=np.float32, sep=' ').reshape((3, 3))
    # TODO: assume the feature is 1/4 of the original image size
    #  check this (MVS used, but Cascade not used)
    # intrinsics[:2, :] /= 4
    intrinsics[:2, :] /= 2
    return intrinsics, extrinsics


# read an image
def read_img(filename):
    img = Image.open(filename)
    # scale 0~255 to 0~1
    np_img = np.array(img, dtype=np.float32) / 255.
    return np_img


def scale_image(image, scale=1, interpolation='linear'):
    """ resize image using cv2 """
    if interpolation == 'linear':
        return cv2.resize(image, None, fx=scale, fy=scale, interpolation=cv2.INTER_LINEAR)
    if interpolation == 'nearest':
        return cv2.resize(image, None, fx=scale, fy=scale, interpolation=cv2.INTER_NEAREST)


def scale_mvs_input(images, scale=1):
    """ resize input to fit into the memory """
    new_images = np.array(scale_image(images, scale=scale))

    return new_images


def crop_mvs_input(images, max_h=1200, max_w=1600, base_image_size=8):
    """ resize images and cameras to fit the network (can be divided by base image size) """
    h, w = images.shape[0:2]
    new_h = h
    new_w = w
    if new_h > max_h:
        new_h = max_h
    else:
        new_h = int(math.ceil(h / base_image_size) * base_image_size)
    if new_w > max_w:
        new_w = max_w
    else:
        new_w = int(math.ceil(w / base_image_size) * base_image_size)
    start_h = int(math.ceil((h - new_h) / 2))
    start_w = int(math.ceil((w - new_w) / 2))
    finish_h = start_h + new_h
    finish_w = start_w + new_w

    new_images = images[start_h:finish_h, start_w:finish_w]

    return new_images


def read_img_resize_crop(filename, max_h=600, max_w=800, base_image_size=8):
    img = Image.open(filename)
    # scale 0~255 to 0~1
    img = np.array(img, dtype=np.float32) / 255.

    h_scale = 0
    w_scale = 0
    height_scale = float(max_h) / img.shape[0]
    width_scale = float(max_w) / img.shape[1]
    if height_scale > h_scale:
        h_scale = height_scale
    if width_scale > w_scale:
        w_scale = width_scale
    if h_scale > 1 or w_scale > 1:
        print("max_h, max_w should < W and H!")
        exit(-1)
    resize_scale = h_scale
    if w_scale > h_scale:
        resize_scale = w_scale

    scaled_input_imgs = scale_mvs_input(img, scale=resize_scale)
    print('scaled_shape', scaled_input_imgs.shape)

    # TODO crop to fit network
    croped_imgs = crop_mvs_input(scaled_input_imgs, max_h=max_h, max_w=max_w, base_image_size=base_image_size)
    print('cropped_shape', croped_imgs.shape)

    return croped_imgs


# read a binary mask
def read_mask(filename):
    return read_img(filename) > 0.5


# save a binary mask
def save_mask(filename, mask):
    assert mask.dtype == np.bool
    mask = mask.astype(np.uint8) * 255
    Image.fromarray(mask).save(filename)


# read a pair file, [(ref_view1, [src_view1-1, ...]), (ref_view2, [src_view2-1, ...]), ...]
def read_pair_file(filename):
    data = []
    with open(filename) as f:
        num_viewpoint = int(f.readline())
        # 49 viewpoints
        for view_idx in range(num_viewpoint):
            ref_view = int(f.readline().rstrip())
            src_views = [int(x) for x in f.readline().rstrip().split()[1::2]]
            if len(src_views) > 0:
                data.append((ref_view, src_views))
    return data


# run MVS model to save depth maps and confidence maps
def save_depth():
    MVSDataset = find_dataset_def(args.dataset)
    test_dataset = MVSDataset(args.testpath, args.testlist, "test", 7, args.numdepth, args.interval_scale,
                              args.inverse_depth, adaptive_scaling=True, max_h=args.max_h, max_w=args.max_w,
                              sample_scale=1, base_image_size=8)

    TestImgLoader = DataLoader(test_dataset, args.batch_size, shuffle=False, num_workers=4, drop_last=False)

    model = AARMVSNet(image_scale=args.image_scale, max_h=args.max_h, max_w=args.max_w, return_depth=args.return_depth)

    # load checkpoint file specified by args.loadckpt
    print("loading model {}".format(args.loadckpt))

    # Allow both keys xxx & module.xxx in dict
    state_dict = torch.load(args.loadckpt)
    if "module.feature.conv0_0.0.weight" in state_dict['model']:
        print("With module in keys")
        model = nn.DataParallel(model)
        model.load_state_dict(state_dict['model'], True)

    else:
        print("No module in keys")
        model.load_state_dict(state_dict['model'], True)
        model = nn.DataParallel(model)
    model.cuda()
    model.eval()

    count = -1
    total_time = 0
    with torch.no_grad():
        for batch_idx, sample in enumerate(TestImgLoader):
            count += 1
            print('process', sample['filename'])
            sample_cuda = tocuda(sample)
            print('input shape: ', sample_cuda["imgs"].shape, sample_cuda["proj_matrices"].shape,
                  sample_cuda["depth_values"].shape)
            time_s = time.time()
            outputs = model(sample_cuda["imgs"], sample_cuda["proj_matrices"], sample_cuda["depth_values"])

            one_time = time.time() - time_s
            total_time += one_time
            print('one forward: ', one_time)
            if count % 50 == 0:
                print('avg time:', total_time / 50)
                total_time = 0

            outputs = tensor2numpy(outputs)
            del sample_cuda
            print('Iter {}/{}'.format(batch_idx, len(TestImgLoader)))
            filenames = sample["filename"]

            # save depth maps and confidence maps
            for filename, depth_est, photometric_confidence in zip(filenames, outputs["depth"],
                                                                   outputs["photometric_confidence"]):
                depth_filename = os.path.join(save_dir, filename.format('depth_est_{}'.format(0), '.pfm'))
                confidence_filename = os.path.join(save_dir, filename.format('confidence_{}'.format(0), '.pfm'))
                os.makedirs(depth_filename.rsplit('/', 1)[0], exist_ok=True)
                os.makedirs(confidence_filename.rsplit('/', 1)[0], exist_ok=True)
                # save depth maps
                print(depth_est.shape)
                save_pfm(depth_filename, depth_est.squeeze())
                # save confidence maps
                save_pfm(confidence_filename, photometric_confidence.squeeze())


# project the reference point cloud into the source view, then project back
def reproject_with_depth(
    depth_ref: np.ndarray,
    intrinsics_ref: np.ndarray,
    extrinsics_ref: np.ndarray,
    depth_src: np.ndarray,
    intrinsics_src: np.ndarray,
    extrinsics_src: np.ndarray,
):
    """Project the reference points to the source view, then project back to calculate the reprojection error

    Args:
        depth_ref: depths of points in the reference view, of shape (H, W)
        intrinsics_ref: camera intrinsic of the reference view, of shape (3, 3)
        extrinsics_ref: camera extrinsic of the reference view, of shape (4, 4)
        depth_src: depths of points in the source view, of shape (H, W)
        intrinsics_src: camera intrinsic of the source view, of shape (3, 3)
        extrinsics_src: camera extrinsic of the source view, of shape (4, 4)

    Returns:
        A tuble contains
            depth_reprojected: reprojected depths of points in the reference view, of shape (H, W)
            x_reprojected: reprojected x coordinates of points in the reference view, of shape (H, W)
            y_reprojected: reprojected y coordinates of points in the reference view, of shape (H, W)
            x_src: x coordinates of points in the source view, of shape (H, W)
            y_src: y coordinates of points in the source view, of shape (H, W)
    """
    width, height = depth_ref.shape[1], depth_ref.shape[0]
    ## step1. project reference pixels to the source view
    # reference view x, y
    x_ref, y_ref = np.meshgrid(np.arange(0, width), np.arange(0, height))
    x_ref, y_ref = x_ref.reshape([-1]), y_ref.reshape([-1])
    # reference 3D space
    xyz_ref = np.matmul(np.linalg.inv(intrinsics_ref),
                        np.vstack((x_ref, y_ref, np.ones_like(x_ref))) * depth_ref.reshape([-1]))
    # source 3D space
    xyz_src = np.matmul(np.matmul(extrinsics_src, np.linalg.inv(extrinsics_ref)),
                        np.vstack((xyz_ref, np.ones_like(x_ref))))[:3]
    # source view x, y
    K_xyz_src = np.matmul(intrinsics_src, xyz_src)
    xy_src = K_xyz_src[:2] / K_xyz_src[2:3]

    ## step2. reproject the source view points with source view depth estimation
    # find the depth estimation of the source view
    x_src = xy_src[0].reshape([height, width]).astype(np.float32)
    y_src = xy_src[1].reshape([height, width]).astype(np.float32)
    sampled_depth_src = cv2.remap(depth_src, x_src, y_src, interpolation=cv2.INTER_LINEAR)
    # mask = sampled_depth_src > 0

    # source 3D space
    # NOTE that we should use sampled source-view depth_here to project back
    xyz_src = np.matmul(np.linalg.inv(intrinsics_src),
                        np.vstack((xy_src, np.ones_like(x_ref))) * sampled_depth_src.reshape([-1]))
    # reference 3D space
    xyz_reprojected = np.matmul(np.matmul(extrinsics_ref, np.linalg.inv(extrinsics_src)),
                                np.vstack((xyz_src, np.ones_like(x_ref))))[:3]
    # source view x, y, depth
    depth_reprojected = xyz_reprojected[2].reshape([height, width]).astype(np.float32)
    K_xyz_reprojected = np.matmul(intrinsics_ref, xyz_reprojected)
    xy_reprojected = K_xyz_reprojected[:2] / K_xyz_reprojected[2:3]
    x_reprojected = xy_reprojected[0].reshape([height, width]).astype(np.float32)
    y_reprojected = xy_reprojected[1].reshape([height, width]).astype(np.float32)

    return depth_reprojected, x_reprojected, y_reprojected, x_src, y_src


def check_geometric_consistency(depth_ref, intrinsics_ref, extrinsics_ref, depth_src, intrinsics_src, extrinsics_src):
    """Check geometric consistency and return valid points

    Args:
        depth_ref: depths of points in the reference view, of shape (H, W)
        intrinsics_ref: camera intrinsic of the reference view, of shape (3, 3)
        extrinsics_ref: camera extrinsic of the reference view, of shape (4, 4)
        depth_src: depths of points in the source view, of shape (H, W)
        intrinsics_src: camera intrinsic of the source view, of shape (3, 3)
        extrinsics_src: camera extrinsic of the source view, of shape (4, 4)

    Returns:
        Tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
            mask: mask for points with geometric consistency, of shape (H, W)
            depth_reprojected: reprojected depths of points in the reference view, of shape (H, W)
            x2d_src: x coordinates of points in the source view, of shape (H, W)
            y2d_src: y coordinates of points in the source view, of shape (H, W)
    """
    width, height = depth_ref.shape[1], depth_ref.shape[0]
    x_ref, y_ref = np.meshgrid(np.arange(0, width), np.arange(0, height))
    depth_reprojected, x2d_reprojected, y2d_reprojected, x2d_src, y2d_src = reproject_with_depth(
        depth_ref, intrinsics_ref, extrinsics_ref, depth_src, intrinsics_src, extrinsics_src)
    # check |p_reproj-p_1| < 1
    dist = np.sqrt((x2d_reprojected - x_ref) ** 2 + (y2d_reprojected - y_ref) ** 2)

    # check |d_reproj-d_1| / d_1 < 0.01
    depth_diff = np.abs(depth_reprojected - depth_ref)
    relative_depth_diff = depth_diff / depth_ref

    mask = np.logical_and(dist < 1, relative_depth_diff < 0.01)
    depth_reprojected[~mask] = 0

    return mask, depth_reprojected, x2d_src, y2d_src


def filter_depth(scan_folder, out_folder, plyfilename):
    # the pair file
    pair_file = os.path.join(scan_folder, "pair.txt")
    # for the final point cloud
    vertexs = []
    vertex_colors = []

    pair_data = read_pair_file(pair_file)
    nviews = len(pair_data)
    print('pair_data 0', pair_data[0])

    # for each reference view and the corresponding source views
    for ref_view, src_views in pair_data:
        # src_views = src_views[:args.num_view]
        # load the camera parameters
        ref_intrinsics, ref_extrinsics = read_camera_parameters(
            os.path.join(scan_folder, 'cams/{:0>8}_cam.txt'.format(ref_view)))
        # load the reference image
        ref_img = read_img_resize_crop(os.path.join(scan_folder, 'images/{:0>8}.jpg'.format(ref_view)))
        print('img shape', ref_img.shape)
        # load the estimated depth of the reference view
        ref_depth_est = read_pfm(os.path.join(out_folder, 'depth_est_0/{:0>8}.pfm'.format(ref_view)))[0]
        print('ref_depth_est shape', ref_depth_est.shape, ref_depth_est[:3, :3])
        # load the photometric mask of the reference view
        confidence = read_pfm(os.path.join(out_folder, 'confidence_0/{:0>8}.pfm'.format(ref_view)))[0]
        print('confidence shape', confidence.shape, confidence.mean())
        # photo_mask = confidence > args.conf  # TODO: check （Cas = 0.9, MVS = 0.8)
        conf = min(0.4, confidence.mean())
        photo_mask = confidence > 0.1  # TODO: check （Cas = 0.9, MVS = 0.8)

        all_srcview_depth_ests = []
        all_srcview_x = []
        all_srcview_y = []
        all_srcview_geomask = []

        # compute the geometric mask
        geo_mask_sum = 0
        for src_view in src_views:
            # camera parameters of the source view
            src_intrinsics, src_extrinsics = read_camera_parameters(
                os.path.join(scan_folder, 'cams/{:0>8}_cam.txt'.format(src_view)))
            # the estimated depth of the source view
            src_depth_est = read_pfm(os.path.join(out_folder, 'depth_est_0/{:0>8}.pfm'.format(src_view)))[0]
            # print('src_depth_est shape', src_depth_est.shape)

            geo_mask, depth_reprojected, x2d_src, y2d_src = check_geometric_consistency(ref_depth_est, ref_intrinsics,
                                                                                        ref_extrinsics, src_depth_est,
                                                                                        src_intrinsics, src_extrinsics)
            # print('geo_mask shape', geo_mask.shape, geo_mask.mean())
            geo_mask_sum += geo_mask.astype(np.int32)
            all_srcview_depth_ests.append(depth_reprojected)
            all_srcview_x.append(x2d_src)
            all_srcview_y.append(y2d_src)
            all_srcview_geomask.append(geo_mask)

        depth_est_averaged = (sum(all_srcview_depth_ests) + ref_depth_est) / (geo_mask_sum + 1)
        # at least 3 source views matched
        # geo_mask = geo_mask_sum >= args.thres_view  # TODO: check (Cas = 5, MVS = 3)
        geo_mask = geo_mask_sum >= 3  # TODO: check (Cas = 5, MVS = 3)
        final_mask = np.logical_and(photo_mask, geo_mask)

        os.makedirs(os.path.join(out_folder, "mask"), exist_ok=True)
        save_mask(os.path.join(out_folder, "mask/{:0>8}_photo.png".format(ref_view)), photo_mask)
        save_mask(os.path.join(out_folder, "mask/{:0>8}_geo.png".format(ref_view)), geo_mask)
        save_mask(os.path.join(out_folder, "mask/{:0>8}_final.png".format(ref_view)), final_mask)

        print("processing {}, ref-view{:0>2}, photo/geo/final-mask:{}/{}/{}".format(scan_folder, ref_view,
                                                                                    photo_mask.mean(), geo_mask.mean(),
                                                                                    final_mask.mean()))

        height, width = depth_est_averaged.shape[:2]
        x, y = np.meshgrid(np.arange(0, width), np.arange(0, height))
        # valid_points = np.logical_and(final_mask, ~used_mask[ref_view])
        valid_points = final_mask
        print("valid_points", valid_points.mean())
        x, y, depth = x[valid_points], y[valid_points], depth_est_averaged[valid_points]
        # color = ref_img[1:-16:4, 1::4, :][valid_points]  # hardcoded for DTU dataset
        # color = ref_img[1:-16:2, 1::2, :][valid_points]  # hardcoded for DTU dataset
        color = ref_img[1::2, 1::2, :][valid_points]  # hardcoded for DTU dataset

        # if num_stage == 1:
        #     color = ref_img[1::4, 1::4, :][valid_points]
        # elif num_stage == 2:
        #     color = ref_img[1::2, 1::2, :][valid_points]
        # elif num_stage == 3:
        #     color = ref_img[valid_points]

        xyz_ref = np.matmul(np.linalg.inv(ref_intrinsics), np.vstack((x, y, np.ones_like(x))) * depth)
        xyz_world = np.matmul(np.linalg.inv(ref_extrinsics), np.vstack((xyz_ref, np.ones_like(x))))[:3]
        vertexs.append(xyz_world.transpose((1, 0)))
        vertex_colors.append((color * 255).astype(np.uint8))

    vertexs = np.concatenate(vertexs, axis=0)
    vertex_colors = np.concatenate(vertex_colors, axis=0)
    vertexs = np.array([tuple(v) for v in vertexs], dtype=[('x', 'f4'), ('y', 'f4'), ('z', 'f4')])
    vertex_colors = np.array([tuple(v) for v in vertex_colors], dtype=[('red', 'u1'), ('green', 'u1'), ('blue', 'u1')])

    vertex_all = np.empty(len(vertexs), vertexs.dtype.descr + vertex_colors.dtype.descr)
    for prop in vertexs.dtype.names:
        vertex_all[prop] = vertexs[prop]
    for prop in vertex_colors.dtype.names:
        vertex_all[prop] = vertex_colors[prop]

    el = PlyElement.describe(vertex_all, 'vertex')
    PlyData([el]).write(plyfilename)
    print("saving the final model to", plyfilename)


if __name__ == '__main__':
    # step1. save all the depth maps and the masks in outputs directory
    print('******************* save depth *******************\n')
    save_depth()

    with open(args.testlist) as f:
        scans = f.readlines()
        scans = [line.rstrip() for line in scans]

    for scan in scans:
        scan_id = int(scan[4:])
        scan_folder = os.path.join(args.testpath, scan)
        out_folder = os.path.join(args.outdir, scan)
        # step2. filter saved depth maps with photometric confidence maps and geometric constraints
        filter_depth(scan_folder, out_folder, os.path.join(args.outdir, 'aa-rmvsnet{:0>3}_l3_6_min0.4.ply'.format(scan_id)))

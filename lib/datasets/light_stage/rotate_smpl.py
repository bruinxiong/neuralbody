import torch.utils.data as data
from lib.utils import base_utils
from PIL import Image
import numpy as np
import json
import os
import imageio
import cv2
from lib.config import cfg
from lib.utils.if_nerf import if_nerf_data_utils as if_nerf_dutils
from plyfile import PlyData
from lib.utils import render_utils


class Dataset(data.Dataset):
    def __init__(self, data_root, human, ann_file, split):
        super(Dataset, self).__init__()

        self.data_root = data_root
        self.human = human
        self.split = split

        annots = np.load(ann_file, allow_pickle=True).item()
        self.cams = annots['cams']

        K, RT = render_utils.load_cam(ann_file)
        render_w2c = RT

        self.ts = np.arange(0, np.pi * 2, np.pi / 72)
        self.nt = len(self.ts)

        i = 0
        i = i + cfg.begin_i
        self.ims = np.array([
            np.array(ims_data['ims'])[cfg.training_view]
            for ims_data in annots['ims'][i:i + cfg.ni * cfg.i_intv]
        ])

        self.K = K[0]
        self.render_w2c = render_w2c
        img_root = 'data/render/{}'.format(cfg.exp_name)
        # base_utils.write_K_pose_inf(self.K, self.render_w2c, img_root)

        self.Ks = np.array(K)[cfg.training_view].astype(np.float32)
        self.RT = np.array(RT)[cfg.training_view].astype(np.float32)
        self.center_rayd = [
            render_utils.get_center_rayd(K_, RT_)
            for K_, RT_ in zip(self.Ks, self.RT)
        ]

        self.Ds = np.array(self.cams['D'])[cfg.training_view].astype(
            np.float32)

        self.nrays = cfg.N_rand

    def get_nearest_cam(self, K, RT):
        center_rayd = render_utils.get_center_rayd(K, RT)
        sim = [np.dot(center_rayd, cam_rayd) for cam_rayd in self.center_rayd]
        return self.RT[np.argmax(sim)]

    def prepare_input(self, i, index):
        i = i + cfg.begin_i

        # read xyz, normal, color from the ply file
        vertices_path = os.path.join(self.data_root, cfg.vertices,
                                     '{}.npy'.format(i))
        xyz = np.load(vertices_path).astype(np.float32)
        nxyz = np.zeros_like(xyz).astype(np.float32)

        # rotate smpl
        t = self.ts[index]
        rot_ = np.array([[np.cos(t), -np.sin(t)], [np.sin(t), np.cos(t)]])
        rot = np.eye(3)
        rot[[0, 0, 1, 1], [0, 1, 0, 1]] = rot_.ravel()
        center = np.mean(xyz, axis=0)
        xyz = xyz - center
        xyz = np.dot(xyz, rot.T)
        xyz = xyz + center
        xyz = xyz.astype(np.float32)

        # obtain the origin bounds for point sampling
        min_xyz = np.min(xyz, axis=0)
        max_xyz = np.max(xyz, axis=0)
        if cfg.big_box:
            min_xyz -= 0.05
            max_xyz += 0.05
        else:
            min_xyz[2] -= 0.05
            max_xyz[2] += 0.05
        can_bounds = np.stack([min_xyz, max_xyz], axis=0)

        # transform smpl from the world coordinate to the smpl coordinate
        params_path = os.path.join(self.data_root, cfg.params,
                                   '{}.npy'.format(i))
        params = np.load(params_path, allow_pickle=True).item()
        Rh = params['Rh']
        R = cv2.Rodrigues(Rh)[0].astype(np.float32)
        R = np.dot(rot, R)
        Rh = cv2.Rodrigues(R)[0]
        Th = params['Th'].astype(np.float32)
        Th = np.sum(rot * (Th - center), axis=1) + center
        Th = Th.astype(np.float32)
        xyz = np.dot(xyz - Th, R).astype(np.float32)

        min_xyz = np.min(xyz, axis=0)
        max_xyz = np.max(xyz, axis=0)
        if cfg.big_box:
            min_xyz -= 0.05
            max_xyz += 0.05
        else:
            min_xyz[2] -= 0.05
            max_xyz[2] += 0.05
        bounds = np.stack([min_xyz, max_xyz], axis=0)

        # move the point cloud to the canonical frame, which eliminates the influence of translation
        cxyz = xyz.astype(np.float32)
        nxyz = nxyz.astype(np.float32)
        feature = np.concatenate([cxyz, nxyz], axis=1).astype(np.float32)

        # construct the coordinate
        dhw = xyz[:, [2, 1, 0]]
        min_dhw = min_xyz[[2, 1, 0]]
        max_dhw = max_xyz[[2, 1, 0]]
        voxel_size = np.array(cfg.voxel_size)
        coord = np.round((dhw - min_dhw) / voxel_size).astype(np.int32)

        # construct the output shape
        out_sh = np.ceil((max_dhw - min_dhw) / voxel_size).astype(np.int32)
        x = 32
        out_sh = (out_sh | (x - 1)) + 1

        return feature, coord, out_sh, can_bounds, bounds, Rh, Th

    def get_mask(self, i):
        ims = self.ims[i]
        msks = []

        for nv in range(len(ims)):
            im = ims[nv]

            msk_path = os.path.join(self.data_root, 'mask', im)[:-4] + '.png'
            msk = imageio.imread(msk_path)
            msk = (msk != 0).astype(np.uint8)

            msk_path = os.path.join(self.data_root, 'mask_cihp',
                                    im)[:-4] + '.png'
            msk_cihp = imageio.imread(msk_path)
            msk_cihp = (msk_cihp != 0).astype(np.uint8)

            msk = (msk | msk_cihp).astype(np.uint8)

            K = self.Ks[nv].copy()
            K[:2] = K[:2] / cfg.ratio
            msk = cv2.undistort(msk, K, self.Ds[nv])

            border = 5
            kernel = np.ones((border, border), np.uint8)
            msk = cv2.dilate(msk.copy(), kernel)

            msks.append(msk)

        return msks

    def __getitem__(self, index):
        i = cfg.i
        feature, coord, out_sh, can_bounds, bounds, Rh, Th = self.prepare_input(
            i, index)

        if self.human in ['CoreView_313', 'CoreView_315']:
            i = cfg.i - 1
        msks = self.get_mask(i)

        # reduce the image resolution by ratio
        H, W = int(cfg.H * cfg.ratio), int(cfg.W * cfg.ratio)
        msks = [
            cv2.resize(msk, (W, H), interpolation=cv2.INTER_NEAREST)
            for msk in msks
        ]
        msks = np.array(msks)
        K = self.K

        ray_o, ray_d, near, far, center, scale, mask_at_box = render_utils.image_rays(
            self.render_w2c[0], K, can_bounds)
        # view_RT = self.get_nearest_cam(K, self.render_w2c[index])
        # ray_d0 = render_utils.get_image_rays0(view_RT,
        #                                       self.render_w2c[index], K,
        #                                       can_bounds)

        ret = {
            'feature': feature,
            'coord': coord,
            'out_sh': out_sh,
            'ray_o': ray_o,
            'ray_d': ray_d,
            # 'ray_d0': ray_d0,
            'near': near,
            'far': far,
            'mask_at_box': mask_at_box
        }

        R = cv2.Rodrigues(Rh)[0].astype(np.float32)
        ind = i
        i = int(np.round(i / cfg.i_intv))
        i = min(i, cfg.ni - 1)
        meta = {
            'bounds': bounds,
            'R': R,
            'Th': Th,
            'i': i,
            'index': index,
            'ind': ind
        }
        ret.update(meta)

        meta = {'msks': msks, 'Ks': self.Ks, 'RT': self.RT}
        ret.update(meta)

        return ret

    def __len__(self):
        return self.nt

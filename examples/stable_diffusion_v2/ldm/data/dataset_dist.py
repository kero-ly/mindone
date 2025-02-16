import moxing as mox
import json as js
import pdb
import numpy as np
from .sync_data import sync_data
import os

def get_split(num_samples, num_devices, device_id, num_parts, max_tars):

    samples_per_device = num_samples.sum() // num_devices
    print('full data samples: ', num_samples.sum())
    print('avg data sample of per device: ', samples_per_device)

    start_part_idx = -1
    start_tar_idx = -1
    start_sample_idx = -1

    cur_device_id = device_id
    start_global_sample_idx = samples_per_device * cur_device_id
    end_global_sample_idx = samples_per_device * (cur_device_id+1) - 1
    #print(start_global_sample_idx, end_global_sample_idx)

    p1 = 0 # pointer 1 towards allocation segment head
    p2 = 0
    for j in range(num_parts):
        for k in range(max_tars):
            p2 += num_samples[j][k]
            if p1 <= start_global_sample_idx < p2:
                start_part_idx = j
                start_tar_idx = k
                start_sample_idx = start_global_sample_idx - p1
                print('find start')

            if start_part_idx != -1:
                if p1 <= end_global_sample_idx < p2:
                    end_part_idx = j
                    end_tar_idx = k
                    end_sample_idx = end_global_sample_idx - p1
                    print('find end')

            p1 = p2
    return (start_part_idx, start_tar_idx, start_sample_idx), (end_part_idx, end_tar_idx, end_sample_idx)


def calculate_split(num_devices, rank_id):
    data_path = "mindone/examples/stable_diffusion_v2/ldm/data/num_samples_parts14.json"
    #data_path = "./ldm/data/num_samples_parts14.json"
    part_tar_samples = js.load(open(data_path))
    part_tar_samples_np = np.zeros((len(part_tar_samples.keys()), 533)).astype(np.int)
    for i, (part_name, part_value) in enumerate(part_tar_samples.items()):
        for j, (tar_name, tar_value) in enumerate(part_value.items()):
            part_tar_samples_np[i][j] = tar_value

    num_parts = part_tar_samples_np.shape[0]
    max_tars = part_tar_samples_np.shape[1]

    start, end = get_split(part_tar_samples_np, num_devices, rank_id, num_parts, max_tars)

    start_part_idx, start_tar_idx, start_sample_idx = start
    end_part_idx, end_tar_idx, end_sample_idx = end

    tars_to_sync = {}
    if start_part_idx == end_part_idx:
        tars_to_sync[start_part_idx] = list(range(start_tar_idx, end_tar_idx +1)) # need add 1 for last tar
    else:
        for j in range(start_part_idx, end_part_idx+1):
            if j == start_part_idx:
                tars_to_sync[j] = list(range(start_tar_idx, max_tars))
            elif j == end_part_idx:
                tars_to_sync[j] = list(range(0, end_tar_idx +1)) # need add 1 for last tar
            else:
                tars_to_sync[j] = list(range(0, max_tars))

    print("Split result:\nStart: ", start_part_idx, start_tar_idx, start_sample_idx)
    print("End: ", end_part_idx, end_tar_idx, end_sample_idx)
    print('tars to sync: ', tars_to_sync)
    return start_part_idx, start_tar_idx, start_sample_idx, end_part_idx, end_tar_idx, end_sample_idx, tars_to_sync

def download_tar(tars_to_sync, start_sample_idx, end_sample_idx):
    mapping = {0: 1,
               1: 2,
               2: 3,
               3: 4,
               4: 6,
               5: 7,
               6: 17,
               7: 33,
               8: 35,
               9: 49,
               10: 50,
               11: 52,
               12: 54}
    src_list = []
    dst_list = []
    for part, tars in tars_to_sync.items():
        for t in tars:
            src_list.append(f"obs://laion-2b-en/sd2.1_base_train/part_{mapping[part]}/{t:05d}.tar")
            dst_list.append(f"/cache/part_{mapping[part]}/{t:05d}.tar")
            os.makedirs(f"/cache/part_{mapping[part]}", exist_ok=True)
    #print(src_list)
    #print(dst_list)
    sync_data(src_list, dst_list, start_sample_idx, end_sample_idx)

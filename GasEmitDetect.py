import cv2 as cv
import torch.nn.functional as F
import torch
import numpy as np

from GasFlowRate import GasFlowRate
from grad_cam_viz import GradCam
from i3d_learner import I3dLearner

DEBUG_MODE = True

class GasEmitDetect:

    def __init__(self, model_addr):

        # I3dLearner Configurations
        if DEBUG_MODE:
            use_cuda = False
            parallel = False
        else:
            use_cuda = True
            parallel = True
        rank = 0
        world_size = 1

        # Set learner and transform
        self.learner = I3dLearner(mode="rgb", use_cuda=use_cuda, parallel=parallel)
        self.transform = self.learner.get_transform(self.learner.mode, image_size=self.learner.image_size)

        # Set model
        self.model = self.learner.set_model(rank, world_size, self.learner.mode, model_addr, parallel, phase="test")
        self.model.train(False)  # set model to evaluate mode (IMPORTANT)
        self.grad_cam = GradCam(self.model, use_cuda=self.learner.use_cuda, normalize=False)


    # -----------------------------------------------------------------------
    # Contrast Limited Adaptive Histogram Equalization ----------------------
    # -----------------------------------------------------------------------
    def Normalize_CLAHE(self, img):
        lab = cv.cvtColor(img, cv.COLOR_BGR2LAB)
        l, a, b = cv.split(lab)
        clahe = cv.createCLAHE(clipLimit=3.0, tileGridSize=(8, 8))
        cl = clahe.apply(l)
        limg = cv.merge((cl, a, b))
        final = cv.cvtColor(limg, cv.COLOR_LAB2BGR)
        return final

    # -----------------------------------------------------------------------
    # Gas Detect (from Video) -----------------------------------------------
    # -----------------------------------------------------------------------
    def DetectGasEmit_from_video(self, in_vid_addr, calc_flow_rate=False, out_vid_addr=None, full_resolution=True, normalize_frame=False, tracking_mode=False):

        capture = cv.VideoCapture(in_vid_addr)
        num_frame = capture.get(cv.CAP_PROP_FRAME_COUNT)
        fps = capture.get(cv.CAP_PROP_FPS)  #frame per secend
        width = int(capture.get(3))
        height = int(capture.get(4))

        print('video info: ', in_vid_addr, ' fps: ', fps, 'frames count: ', num_frame, ' W:', width, ' H:', width)
        if num_frame < 1:
            print(f'{in_vid_addr} doesnt exist!')
            return

        # initialize output video file
        if out_vid_addr is not None:
            fourcc = cv.VideoWriter_fourcc(*'mp4v')
            out_video = cv.VideoWriter(out_vid_addr, fourcc, fps, (width, 2 * height))


        #use MOG to fixed background
        fgbg = cv.createBackgroundSubtractorMOG2(history=500, detectShadows=False)


        nf = 24                 # frame number of video to detect smoke
        nf_ovl = int(nf / 2)    # nf overlap

        smoke_check_frame = 224
        if not full_resolution:
            smoke_check_frame = min(width, height)

        rgb_4d_smoke_hist = None
        gas_emit_report = []


        if calc_flow_rate:
            gfr_obj = GasFlowRate(fps, nf)


        rgb_4d = np.zeros((nf, height, width, 3), dtype=np.float32)
        all_frames = np.zeros((nf, height, width, 3), dtype=np.uint8)
        gray_frames = np.zeros((nf, height, width), dtype=np.uint8)
        frame_act_3d = np.zeros([nf, height, width])

        for org_frm in range(0, int(num_frame - nf - nf_ovl), nf - nf_ovl):

            for f in range(nf):
                if (org_frm != 0) and (f < nf_ovl):
                    frame = all_frames[nf - nf_ovl + f]
                else:
                    ret, frame = capture.read()

                all_frames[f, :, :, :] = frame

                # convert video frame to RGB
                img_rgb = cv.cvtColor(frame, cv.COLOR_BGR2RGB)
                if normalize_frame:
                    img_rgb = self.Normalize_CLAHE(img_rgb)

                rgb_4d[f, :, :, :] = img_rgb

                # convert video frame to gray-scaled
                gray_frames[f,:,:] = cv.cvtColor(frame, cv.COLOR_BGR2GRAY)

                # GMM (Remove noise and weak camera motion)
                fgmask = fgbg.apply(gray_frames[f,:,:])
                kernel1 = np.ones((5, 5), np.uint8)
                kernel2 = np.ones((3, 3), np.uint8)
                fgmask = cv.erode(fgmask, kernel2)
                fgmask = cv.dilate(fgmask, kernel1)
                frame_act_3d[f, :, :] = fgmask

            frame_act_2d = np.mean(frame_act_3d, axis=0)

            #---------------------------------------------------
            #--- tracking mode ---------------------------------
            #---------------------------------------------------
            if tracking_mode :
                rgb_4d_wrap = np.zeros((nf, height, width, 3), dtype=np.float32)
                flow_hist = np.zeros((nf, height, width, 2), dtype=np.float32)
                shift_frame = np.zeros([nf, 2])

                for i in range(nf - 1):
                    gray = gray_frames[i]
                    gray_next = gray_frames[i + 1]
                    flow = 2.0 * cv.calcOpticalFlowFarneback(gray, gray_next, None, 0.5, 3, 25, 3, 5, 1.1, 0)
                    flow_hist[i, :, :, :] = flow

                    flow_abs = np.abs(flow[:, :, 0] + 1j * flow[:, :, 1])
                    ind = np.unravel_index(np.argmax(flow_abs), flow_abs.shape)
                    flow_max = flow[ind[0], ind[1]]
                    shift_frame[i, :] = flow_max

                    cond = flow_abs > 1.0
                    if len(cond) > 0:
                        flow_sel = flow[cond]
                        shift_frame[i, 0] = np.median(flow_sel[:, 0])
                        shift_frame[i, 1] = np.median(flow_sel[:, 1])
                    else:
                        shift_frame[i, :] = [0, 0]

                shift_frame_acc = np.zeros([nf, 2])
                nf_2 = int(nf / 2)
                for i in range(nf_2):
                    shift_frame_acc[i, :] = np.sum(shift_frame[i:nf_2, :], axis=0)
                for i in range(nf_2 + 1, nf, 1):
                    shift_frame_acc[i, :] = -1 * np.sum(shift_frame[nf_2:i, :], axis=0)

                for i in range(nf):
                    shift_frame_acc_abs = np.abs(shift_frame_acc[i, 0] + 1j * shift_frame_acc[i, 1])
                    if (shift_frame_acc_abs > 5):
                        matrix = [[1, 0, shift_frame_acc[i, 0]],  # x
                                  [0, 1, shift_frame_acc[i, 1]]]  # y
                        t = np.float32(matrix)
                        rgb_4d_wrap[i, :, :, :] = cv.warpAffine(rgb_4d[i, :, :, :], t, (width, height))
                    else:
                        rgb_4d_wrap[i, :, :, :] = rgb_4d[i, :, :, :]

                flow_diff = flow_hist[nf_2, :, :, :]  # - shift_frame
                flow_diff[:, :, 0] = flow_diff[:, :, 0] - shift_frame[nf_2, 0]
                flow_diff[:, :, 1] = flow_diff[:, :, 1] - shift_frame[nf_2, 1]
                flow_abs_after_shift = np.abs(flow_diff[:, :, 0] + 1j * flow_diff[:, :, 1])

                kernel = np.ones((10, 10), np.float32)
                frame_act_2d = cv.filter2D(flow_abs_after_shift, -1, kernel)

            if DEBUG_MODE:
                print('\nframe : ', str(org_frm))

            smoke_thr = 0.6
            activation_thr = 0.85

            rgb_4d_smoke = np.zeros([nf, height, width, 3], dtype=np.uint8)

            w_step = int(np.ceil(1.4 * (width / smoke_check_frame - 1) + 1))
            h_step = int(np.ceil(1.4 * (height / smoke_check_frame - 1) + 1))

            found_and_smoke = False
            for w in range(w_step):
                for h in range(h_step):

                    x1 = w * int(smoke_check_frame * 0.7)
                    y1 = h * int(smoke_check_frame * 0.7)
                    if (x1 >= width) or (y1 >= height):
                        continue
                    x2 = x1 + smoke_check_frame
                    y2 = y1 + smoke_check_frame
                    if x2 > width:
                        x1 = width - smoke_check_frame
                        x2 = width
                    if y2 > height:
                        y1 = height - smoke_check_frame
                        y2 = height

                    activity_sum = np.sum(frame_act_2d[y1:y2, x1:x2])
                    activity_thr = (y2-y1) * (x2-x1) / 100

                    if activity_sum > activity_thr:
                        selected_zone = np.uint8(rgb_4d[:, y1:y2, x1:x2, :])
                        if tracking_mode:
                            selected_zone = np.uint8(rgb_4d_wrap[:, y1:y2, x1:x2, :])

                        v = self.transform(selected_zone)
                        v = torch.unsqueeze(v, 0)
                        if self.learner.use_cuda:
                            v = v.cuda()

                        pred_pre, pred_upsample_pre = self.learner.make_pred(self.model, v, upsample=None)
                        pred = F.softmax(pred_pre.squeeze().transpose(0, 1)).cpu().detach().numpy()[:, 1]
                        pred_upsample = F.softmax(pred_upsample_pre.squeeze().transpose(0, 1)).cpu().detach().numpy()[:,1]
                        smoke_pb = np.median(pred)  # use the median as the probability

                        if smoke_pb > smoke_thr:
                            C = self.grad_cam.generate_cam(v, 1)  # 1 is the target class, which means having smoke emissions
                            C = C.reshape((C.shape[0], -1))

                            active_c = np.multiply(C > activation_thr, 1)
                            smoke_map = np.reshape(active_c, (nf, 224, 224))
                            smoke_map = np.max(smoke_map[10:20, :, :], axis=0)

                            if smoke_check_frame != 224:
                                smoke_map_scaled = cv.resize(np.uint8(smoke_map), (smoke_check_frame, smoke_check_frame))
                            else:
                                smoke_map_scaled = smoke_map

                            for f in range(nf):
                                rgb_4d_smoke[f, y1:y2, x1:x2, 2] = np.maximum(
                                    rgb_4d_smoke[f, y1:y2, x1:x2, 2],
                                    np.uint8(255 * smoke_map_scaled[:, :] * pred_upsample[f]))  # * abs_sel[f,:,:]))


                            # ---------------------------------------------------
                            # tracking mode (reverse tracked frames) ------------
                            # ---------------------------------------------------
                            if tracking_mode:
                                E_Z = np.zeros((height, width))
                                E_T = np.zeros((height, width))
                                for f in range(nf):
                                    E_Z[y1:y2, x1:x2] = smoke_map_scaled

                                    shift_frame_acc_abs = np.abs(shift_frame_acc[f, 0] + 1j * shift_frame_acc[f, 1])
                                    if (shift_frame_acc_abs > 5):
                                        matrix = [[1, 0, -1 * shift_frame_acc[f, 0]],  # x
                                                  [0, 1, -1 * shift_frame_acc[f, 1]]]  # y
                                        t = np.float32(matrix)
                                        E_T = cv.warpAffine(np.float32(E_Z), t, (width, height))
                                        rgb_4d_smoke[f, :, :, 2] = np.maximum(rgb_4d_smoke[f, :, :, 2], np.uint8(
                                            255 * E_T * pred_upsample[f]))
                                    else:
                                        rgb_4d_smoke[f, y1:y2, x1:x2, 2] = np.maximum(
                                            rgb_4d_smoke[f, y1:y2, x1:x2, 2],
                                            np.uint8(255 * smoke_map_scaled[:, :] * pred_upsample[f]))


                            # draw green rect if region
                            rgb_4d_smoke[:, y1:y2, x1, 1] = np.uint8(255 * np.ones([nf, y2 - y1]))
                            rgb_4d_smoke[:, y1:y2, x2 - 1, 1] = np.uint8(255 * np.ones([nf, y2 - y1]))
                            rgb_4d_smoke[:, y1, x1:x2, 1] = np.uint8(255 * np.ones([nf, x2 - x1]))
                            rgb_4d_smoke[:, y2 - 1, x1:x2, 1] = np.uint8(255 * np.ones([nf, x2 - x1]))
                            found_and_smoke = True

            gfr_result = []
            if calc_flow_rate and found_and_smoke:
                # gfr_obj.ClacGasFlowRate(np.uint8(rgb_4d_smoke[f]))
                gfr_result = gfr_obj.CalcGasFlowRate(gray_frames, np.uint8(rgb_4d_smoke[f, :, :, 2]))


            # write the flipped frame
            if out_vid_addr is not None:
                frm_count_write = nf - nf_ovl
                frm_offset_write = nf_ovl
                if org_frm == 0:
                    frm_offset_write = 0


                for f in range(frm_offset_write, frm_offset_write + frm_count_write, 1):

                    merge_frame = np.maximum(rgb_4d_smoke[f], all_frames[f])
                    if calc_flow_rate:
                        for res in gfr_result:
                            rect = res[3]
                            box = cv.boxPoints(rect)
                            box = np.int0(box)
                            merge_frame = cv.drawContours(merge_frame, [box], 0, (255, 255, 255), 1)
                            str_rate = '{:.2f} MCF'.format(res[0])
                            cv.putText(merge_frame, str_rate, (int(res[1]), int(res[2])), cv.FONT_ITALIC, fontScale=1,
                                       thickness=2, lineType=cv.LINE_AA, color=(255, 100, 50))

                    all = np.concatenate((all_frames[f], merge_frame), axis=0)
                    out_video.write(all)

            if DEBUG_MODE:
                cv.imshow('all', all)
                if cv.waitKey(1) & 0xFF == ord('q'):
                    break

        capture.release()
        if out_vid_addr:
            out_video.release()
        cv.destroyAllWindows()

        return gas_emit_report

''' This module will handle the text generation with beam search. '''

import torch
import torch.nn as nn
from torch.autograd import Variable

from transformer.Models import Transformer
from transformer.Beam import Beam


class Decode(object):
    ''' Load with trained model and handle the beam search '''

    def __init__(self, opt, device):
        self.opt = opt
        self.device = device

        checkpoint = torch.load(opt.model)
        model_opt = checkpoint['settings']
        self.model_opt = model_opt

        model = Transformer(
            model_opt.input_dim,
            model_opt.output_dim,
            model_opt.n_inputs_max_seq,
            model_opt.n_outputs_max_seq,
            d_k=model_opt.d_k,
            d_v=model_opt.d_v,
            d_model=model_opt.d_model,
            d_inner_hid=model_opt.d_inner_hid,
            n_layers=model_opt.n_layers,
            n_head=model_opt.n_head,
            dropout=model_opt.dropout,
            device=device,
            is_train=False)

        prob_projection = nn.LogSoftmax()

        model.load_state_dict(checkpoint['model'])
        print('[Info] Trained model state loaded.')

        model.to(device)
        prob_projection.to(device)

        model.prob_projection = prob_projection

        self.model = model
        self.model.eval()

    def decode_batch(self, src_batch):
        ''' Translation work in one batch '''

        # Batch size is in different location depending on data.
        src_seq, src_pos = src_batch
        batch_size = src_seq.size(0)
        beam_size = self.opt.beam_size

        #- Enocde
        enc_output, *_ = self.model.encoder(src_seq, src_pos)

        #--- Repeat data for beam
        src_pos = Variable(
            src_pos.data.repeat(1, beam_size).view(
                src_pos.size(0) * beam_size, src_pos.size(1)))

        enc_output = Variable(
            enc_output.data.repeat(1, beam_size, 1).view(
                enc_output.size(0) * beam_size, enc_output.size(1), enc_output.size(2)))

        #--- Prepare beams
        beams = [Beam(beam_size, self.device) for _ in range(batch_size)]
        beam_inst_idx_map = {
            beam_idx: inst_idx for inst_idx, beam_idx in enumerate(range(batch_size))}
        n_remaining_sents = batch_size

        #- Decode
        for i in range(100):

            len_dec_seq = i + 1

            # -- Preparing decoded data seq -- #
            # size: batch x beam x seq
            dec_partial_seq = torch.stack([
                b.get_current_state() for b in beams if not b.done])
            # size: (batch * beam) x seq
            dec_partial_seq = dec_partial_seq.view(-1, len_dec_seq)
            # -- Preparing decoded pos seq -- #
            # size: 1 x seq
            dec_partial_pos = torch.arange(1, len_dec_seq + 1).unsqueeze(0)
            # size: (batch * beam) x seq
            dec_partial_pos = dec_partial_pos.repeat(
                n_remaining_sents * beam_size, 1)

            dec_partial_seq = dec_partial_seq.to(self.device)
            dec_partial_pos = dec_partial_pos.to(self.device)

            # -- Decoding -- #
            dec_output, *_ = self.model.decoder(
                dec_partial_seq, dec_partial_pos, src_pos, enc_output)
            dec_output = dec_output[:, -1, :]  # (batch * beam) * d_model
            dec_output = self.model.tgt_word_proj(dec_output)
            out = self.model.prob_projection(dec_output)

            # batch x beam x n_words
            word_lk = out.view(n_remaining_sents, beam_size, -1).contiguous()

            active_beam_idx_list = []
            for beam_idx in range(batch_size):
                if beams[beam_idx].done:
                    continue

                inst_idx = beam_inst_idx_map[beam_idx]
                if not beams[beam_idx].advance(word_lk.data[inst_idx]):
                    active_beam_idx_list += [beam_idx]

            if not active_beam_idx_list:
                # all instances have finished their path to <EOS>
                break

            # in this section, the sentences that are still active are
            # compacted so that the decoder is not run on completed sentences
            active_inst_idxs = torch.LongTensor(
                [beam_inst_idx_map[k] for k in active_beam_idx_list])

            # update the idx mapping
            beam_inst_idx_map = {
                beam_idx: inst_idx for inst_idx, beam_idx in enumerate(active_beam_idx_list)}

            def update_active_seq(seq_var, active_inst_idxs):
                ''' Remove the src sequence of finished instances in one batch. '''

                inst_idx_dim_size, *rest_dim_sizes = seq_var.size()
                inst_idx_dim_size = inst_idx_dim_size * \
                    len(active_inst_idxs) // n_remaining_sents
                new_size = (inst_idx_dim_size, *rest_dim_sizes)

                # select the active instances in batch
                original_seq_data = seq_var.data.view(n_remaining_sents, -1)
                active_seq_data = original_seq_data.index_select(
                    0, active_inst_idxs)
                active_seq_data = active_seq_data.view(*new_size)
                with torch.no_grad():
                    return Variable(active_seq_data)

            def update_active_enc_info(enc_info_var, active_inst_idxs):
                ''' Remove the encoder outputs of finished instances in one batch. '''

                inst_idx_dim_size, *rest_dim_sizes = enc_info_var.size()
                inst_idx_dim_size = inst_idx_dim_size * \
                    len(active_inst_idxs) // n_remaining_sents
                new_size = (inst_idx_dim_size, *rest_dim_sizes)

                # select the active instances in batch
                original_enc_info_data = enc_info_var.data.view(
                    n_remaining_sents, -1, self.model_opt.d_model)
                active_enc_info_data = original_enc_info_data.index_select(
                    0, active_inst_idxs)
                active_enc_info_data = active_enc_info_data.view(*new_size)
                with torch.no_grad():
                    return Variable(active_enc_info_data)

            src_pos = update_active_seq(src_pos, active_inst_idxs)
            enc_output = update_active_enc_info(
                enc_output, active_inst_idxs.to(self.device))

            #- update the remaining size
            n_remaining_sents = len(active_inst_idxs)

        #- Return useful information
        all_hyp, all_scores = [], []
        n_best = self.opt.n_best

        for beam_idx in range(batch_size):
            scores, tail_idxs = beams[beam_idx].sort_scores()
            all_scores += [scores[:n_best]]

            hyps = [beams[beam_idx].get_hypothesis(
                i) for i in tail_idxs[:n_best]]
            all_hyp += [hyps]

        return all_hyp, all_scores

import torch
from torch import nn
from torch import distributions

class CaptioningModel(nn.Module):
    def __init__(self):
        super(CaptioningModel, self).__init__()

    def init_weights(self):
        raise NotImplementedError

    def init_state(self, b_s, device):
        raise NotImplementedError

    def step(self, t, state, prev_output, images, seq, *args, mode='teacher_forcing'):
        raise NotImplementedError

    def forward(self, images, seq, *args):
        device = images.device
        b_s = images.size(0)
        seq_len = seq.size(1)
        state = self.init_state(b_s, device)
        out = None

        outputs = []
        for t in range(seq_len):
            out, state = self.step(t, state, out, images, seq, *args, mode='teacher_forcing')
            outputs.append(out)

        outputs = torch.cat([o.unsqueeze(1) for o in outputs], 1)
        return outputs

    def test(self, images, seq_len, *args):
        device = images.device
        b_s = images.size(0)
        state = self.init_state(b_s, device)
        out = None

        outputs = []
        for t in range(seq_len):
            out, state = self.step(t, state, out, images, None, *args, mode='feedback')
            out = torch.max(out, -1)[1]
            outputs.append(out)

        return torch.cat([o.unsqueeze(1) for o in outputs], 1)

    def sample_rl(self, images, seq_len, *args):
        device = images.device
        b_s = images.size(0)
        state = self.init_state(b_s, device)
        out = None

        outputs = []
        log_probs = []
        for t in range(seq_len):
            out, state = self.step(t, state, out, images, None, *args, mode='feedback')
            distr = distributions.Categorical(logits=out)
            out = distr.sample()
            outputs.append(out)
            log_probs.append(distr.log_prob(out))

        return torch.cat([o.unsqueeze(1) for o in outputs], 1), torch.cat([o.unsqueeze(1) for o in log_probs], 1)

    def beam_search(self, images, seq_len, eos_idx, beam_size, out_size=1, *args):
        device = images.device
        b_s = images.size(0)
        state = self.init_state(b_s, device)
        outputs = []

        for i in range(b_s):
            state_i = tuple(s[i:i+1] for s in state)
            images_i = images[i:i+1]
            selected_words = None
            cur_beam_size = beam_size

            outputs_i = []
            logprobs_i = []
            tmp_outputs_i = [[] for _ in range(cur_beam_size)]
            seq_logprob = .0
            for t in range(seq_len):
                word_logprob, state_i = self.step(t, state_i, selected_words, images_i, None, *args, mode='feedback')
                seq_logprob = seq_logprob + word_logprob
                selected_logprob, selected_idx = torch.sort(seq_logprob.view(-1), -1, descending=True)
                selected_logprob, selected_idx = selected_logprob[:cur_beam_size], selected_idx[:cur_beam_size]

                selected_beam = selected_idx / word_logprob.shape[1]
                selected_words = selected_idx - selected_beam*word_logprob.shape[1]

                # Update outputs with sequences that reached EOS
                outputs_i.extend([tmp_outputs_i[x.item()] for x in torch.masked_select(selected_beam, selected_words == eos_idx)])
                logprobs_i.extend([x.item() for x in torch.masked_select(selected_logprob, selected_words == eos_idx)])
                cur_beam_size -= torch.sum(selected_words == eos_idx).item()

                # Remove sequence if it reaches EOS
                selected_beam = torch.masked_select(selected_beam, selected_words != eos_idx)
                selected_logprob = torch.masked_select(selected_logprob, selected_words != eos_idx)
                selected_words = torch.masked_select(selected_words, selected_words != eos_idx)

                tmp_outputs_i = [tmp_outputs_i[x.item()] for x in selected_beam]
                tmp_outputs_i = [o+[selected_words[x].item(),] for x, o in enumerate(tmp_outputs_i)]

                if selected_beam.shape[0] == 0:
                    break

                state_i = tuple(torch.index_select(s, 0, selected_beam) for s in state_i)
                images_i = torch.index_select(images_i, 0, selected_beam)
                seq_logprob = selected_logprob.view(-1, 1)

            # Update outputs with sequences that did not reach EOS
            outputs_i.extend(tmp_outputs_i)
            logprobs_i.extend([x.item() for x in selected_logprob])

            # Sort result
            outputs_i = [x for _,x in sorted(zip(logprobs_i,outputs_i), reverse=True)][:out_size]
            if len(outputs_i) == 1:
                outputs_i = outputs_i[0]
            outputs.append(outputs_i)

        return outputs

    def beam_search_new(self, images, seq_len, eos_idx, beam_size, out_size=1, *args):
        # todo it does not work properly
        # todo non andare avanti se raggiungono eos con altri beam... si riesce a farlo tutto tensoriale?
        device = images.device
        b_s = images.size(0)
        state = self.init_state(b_s, device)

        seq_logprob = .0

        outputs = [[] for _ in range(b_s)]
        logprobs = [[] for _ in range(b_s)]
        tmp_outputs = [[[] for __ in range(beam_size)] for _ in range(b_s)]
        selected_words = None

        for t in range(seq_len):
            cur_beam_size = 1 if t == 0 else beam_size

            word_logprob, state = self.step(t, state, selected_words, images, None, *args, mode='feedback')
            seq_logprob = seq_logprob + word_logprob.view(b_s, cur_beam_size, -1)

            # Remove sequence if it reaches EOS
            if t > 0:
                mask = selected_words.view(b_s, cur_beam_size, -1) == eos_idx
                seq_logprob = (1-mask).float()*seq_logprob
            selected_logprob, selected_idx = torch.sort(seq_logprob.view(b_s, -1), -1, descending=True)
            selected_logprob, selected_idx = selected_logprob[:, :beam_size], selected_idx[:, :beam_size]

            selected_beam = selected_idx / seq_logprob.shape[-1]
            selected_words = selected_idx - selected_beam*seq_logprob.shape[-1]

            # Update outputs with sequences that reached EOS
            for i in range(b_s):
                outputs[i].extend([tmp_outputs[i][x.item()] for x in torch.masked_select(selected_beam[i], selected_words[i] == eos_idx)])
                logprobs[i].extend([x.item() for x in torch.masked_select(selected_logprob[i], selected_words[i] == eos_idx)])
                tmp_outputs[i] = [tmp_outputs[i][x.item()] for x in selected_beam[i]]
                tmp_outputs[i] = [o+[selected_words[i, x].item(),] for x, o in enumerate(tmp_outputs[i])]

            state = tuple(torch.gather(s.view(b_s, cur_beam_size, -1), 1, selected_beam.unsqueeze(-1).expand(b_s, beam_size, s.shape[-1])).view(-1, s.shape[-1]) for s in state)
            seq_logprob = selected_logprob.unsqueeze(-1)
            selected_words = selected_words.view(-1)

        # Update outputs with sequences that did not reach EOS
        for i in range(b_s):
            outputs[i].extend(tmp_outputs[i])
            logprobs[i].extend([x.item() for x in selected_logprob[i]])

            # Sort result
            outputs[i] = [x for _,x in sorted(zip(logprobs[i],outputs[i]), reverse=True)][:out_size]
            if len(outputs[i]) == 1:
                outputs[i] = outputs[i][0]

        return outputs

from __future__ import division
import torch
import torch.nn as nn
from torch.autograd import Variable
import onmt
import onmt.modules
from onmt.IO import ONMTDataset
from onmt.modules import aeq
from onmt.modules.Gate import ContextGateFactory
from torch.nn.utils.rnn import pad_packed_sequence as unpack
from torch.nn.utils.rnn import pack_padded_sequence as pack


class Embeddings(nn.Module):
    """
    Words embeddings dictionary for Encoder/Decoder.

    Args:
        embedding_dim (int): size of the dictionary of embeddings.
        position_encoding (bool): use a sin to mark relative words positions.
        feat_merge (string): merge action for the features embeddings:
                    concat, sum or mlp.
        feat_dim_exponent (float): when using '-feat_merge concat', feature
                    embedding size is N^feat_dim_exponent, where N is the
                    number of values of feature takes.
        feat_embedding_dim (int): embedding dimension for features when using
                    '-feat_merge mlp'
        dropout (float): dropout probablity.
        padding_idx (int): padding index in the embedding dictionary.
        num_word_embeddings (int): size of dictionary of embeddings for words.
        num_feat_embeddings ([int], optional): list of size of dictionary
                                    of embeddings for each feature.
    """
    def __init__(self, embedding_dim, position_encoding, feat_merge,
                 feat_dim_exponent, feat_embedding_dim, dropout,
                 padding_idx,
                 num_word_embeddings, num_feat_embeddings=None):
        super(Embeddings, self).__init__()
        self.positional_encoding = position_encoding
        if self.positional_encoding:
            self.pe = self.make_positional_encodings(embedding_dim, 5000)
            self.dropout = nn.Dropout(p=dropout)

        self.padding_idx = padding_idx
        self.feat_merge = feat_merge

        # num_embeddings: list of size of dictionary of embeddings
        #                 for words and each feature.
        num_embeddings = [num_word_embeddings]
        # embedding_dims: list of dimension of each embedding vector
        #                 for words and each feature.
        embedding_dims = [embedding_dim]
        if num_feat_embeddings:
            num_embeddings.extend(num_feat for num_feat in num_feat_embeddings)
            if feat_merge == 'concat':
                # Derive embedding dims from each feature's vocab size
                embedding_dims.extend([int(num_feat ** feat_dim_exponent)
                                      for num_feat in num_feat_embeddings])
            elif feat_merge == 'sum':
                # All embeddings to be summed must be the same size
                embedding_dims.extend([embedding_dim] *
                                      len(num_feat_embeddings))
            else:
                # mlp feature merge
                embedding_dims.extend([feat_embedding_dim]
                                      * len(num_feat_embeddings))
                # apply a layer of mlp to get it down to the correct dim
                self.mlp = nn.Sequential(onmt.modules.BottleLinear(
                                        sum(embedding_dims),
                                        embedding_dim),
                                        nn.ReLU())
        self.emb_luts = \
            nn.ModuleList([
                nn.Embedding(num_emb, emb_dim, padding_idx=padding_idx)
                for num_emb, emb_dim in zip(num_embeddings, embedding_dims)])

    @property
    def word_lut(self):
        return self.emb_luts[0]

    @property
    def embedding_dim(self):
        """
        Returns sum of all feature dimensions if the merge action is concat.
        Otherwise, returns word vector size.
        """
        if self.feat_merge == 'concat':
            return sum(emb_lut.embedding_dim
                       for emb_lut in self.emb_luts.children())
        else:
            return self.word_lut.embedding_dim

    def make_positional_encodings(self, dim, max_len):
        pe = torch.arange(0, max_len).unsqueeze(1).expand(max_len, dim)
        div_term = 1 / torch.pow(10000, torch.arange(0, dim * 2, 2) / dim)
        pe = pe * div_term.expand_as(pe)
        pe[:, 0::2] = torch.sin(pe[:, 0::2])
        pe[:, 1::2] = torch.cos(pe[:, 1::2])
        return pe.unsqueeze(1)

    def load_pretrained_vectors(self, emb_file):
        if emb_file is not None:
            pretrained = torch.load(emb_file)
            self.word_lut.weight.data.copy_(pretrained)

    def merge(self, features):
        if self.feat_merge == 'concat':
            return torch.cat(features, 2)
        elif self.feat_merge == 'sum':
            return sum(features)
        else:
            return self.mlp(torch.cat(features, 2))

    def forward(self, src_input):
        """
        Return the embeddings for words, and features if there are any.
        Args:
            src_input (LongTensor): len x batch x nfeat
        Return:
            emb (FloatTensor): len x batch x self.embedding_dim
        """
        in_length, in_batch, nfeat = src_input.size()
        aeq(nfeat, len(self.emb_luts))

        if len(self.emb_luts) == 1:
            emb = self.word_lut(src_input.squeeze(2))
        else:
            feat_inputs = (feat.squeeze(2)
                           for feat in src_input.split(1, dim=2))
            features = [lut(feat)
                        for lut, feat in zip(self.emb_luts, feat_inputs)]
            emb = self.merge(features)

        if self.positional_encoding:
            emb = emb + Variable(self.pe[:emb.size(0), :1, :emb.size(2)]
                                 .expand_as(emb))
            emb = self.dropout(emb)

        out_length, out_batch, emb_dim = emb.size()
        aeq(in_length, out_length)
        aeq(in_length, out_length)
        aeq(emb_dim, self.embedding_dim)

        return emb


def build_embeddings(opt, padding_idx, num_word_embeddings,
                     for_encoder, num_feat_embeddings=None):
    """
    Create an Embeddings instance.
    Args:
        opt: command-line options.
        padding_idx(int): padding index in the embedding dictionary.
        num_word_embeddings(int): size of dictionary
                                 of embedding for words.
        for_encoder(bool): make Embeddings for Encoder or Decoder?
        num_feat_embeddings([int]): list of size of dictionary
                                    of embedding for each feature.
    """
    if for_encoder:
        embedding_dim = opt.src_word_vec_size
    else:
        embedding_dim = opt.tgt_word_vec_size
    return Embeddings(embedding_dim,
                      opt.position_encoding,
                      opt.feat_merge,
                      opt.feat_vec_exponent,
                      opt.feat_vec_size,
                      opt.dropout,
                      padding_idx,
                      num_word_embeddings,
                      num_feat_embeddings)


class Encoder(nn.Module):
    """
    Encoder recurrent neural network.
    """
    def __init__(self, encoder_type, bidirectional, rnn_type,
                 num_layers, rnn_size, dropout, embeddings):
        """
        Args:
            encoder_type (string): rnn, brnn, mean, or transformer.
            bidirectional (bool): bidirectional Encoder.
            rnn_type (string): LSTM or GRU.
            num_layers (int): number of Encoder layers.
            rnn_size (int): size of hidden states of a rnn.
            dropout (float): dropout probablity.
            embeddings (Embeddings): vocab embeddings for this Encoder.
        """
        # Call nn.Module.__init().
        super(Encoder, self).__init__()

        # Basic attributes.
        self.encoder_type = encoder_type
        self.num_directions = 2 if bidirectional else 1
        assert rnn_size % self.num_directions == 0
        self.num_layers = num_layers
        self.hidden_size = rnn_size // self.num_directions
        self.embeddings = embeddings

        # Build the Encoder RNN.
        if self.encoder_type == "transformer":
            padding_idx = embeddings.padding_idx
            self.transformer = nn.ModuleList(
                [onmt.modules.TransformerEncoder(
                        self.hidden_size, dropout, padding_idx)
                 for i in range(self.num_layers)])
        else:
            self.rnn = getattr(nn, rnn_type)(
                 input_size=self.embeddings.embedding_dim,
                 hidden_size=self.hidden_size,
                 num_layers=self.num_layers,
                 dropout=dropout,
                 bidirectional=bidirectional)

    def forward(self, input, lengths=None, hidden=None):
        """
        Args:
            input (LongTensor): len x batch x nfeat
            lengths (LongTensor): batch
            hidden: Initial hidden state.

        Returns:
            hidden_t (FloatTensor): Pair of layers x batch x rnn_size - final
                                    Encoder state
            outputs (FloatTensor):  len x batch x rnn_size -  Memory bank
        """
        # CHECKS
        s_len, n_batch, n_feats = input.size()
        if lengths is not None:
            n_batch_, = lengths.size()
            aeq(n_batch, n_batch_)
        # END CHECKS

        emb = self.embeddings(input)
        s_len, n_batch, emb_dim = emb.size()

        if self.encoder_type == "mean":
            # No RNN, just take mean as final state.
            mean = emb.mean(0) \
                   .expand(self.num_layers, n_batch, emb_dim)
            return (mean, mean), emb

        elif self.encoder_type == "transformer":
            # Self-attention tranformer.
            out = emb.transpose(0, 1).contiguous()
            for i in range(self.num_layers):
                out = self.transformer[i](out, input[:, :, 0].transpose(0, 1))
            return Variable(emb.data), out.transpose(0, 1).contiguous()
        else:
            # Standard RNN encoder.
            packed_emb = emb
            if lengths is not None:
                # Lengths data is wrapped inside a Variable.
                lengths = lengths.view(-1).tolist()
                packed_emb = pack(emb, lengths)
            outputs, hidden_t = self.rnn(packed_emb, hidden)
            if lengths:
                outputs = unpack(outputs)[0]
            return hidden_t, outputs


class Decoder(nn.Module):
    """
    Decoder + Attention recurrent neural network.
    """

    def __init__(self, opt, embeddings):
        """
        Args:
            opt: model options
            dicts: Target `Dict` object
        """
        self.layers = opt.dec_layers
        self.decoder_type = opt.decoder_type
        self._coverage = opt.coverage_attn
        self.hidden_size = opt.rnn_size
        self.input_feed = opt.input_feed
        input_size = opt.tgt_word_vec_size
        if self.input_feed:
            input_size += opt.rnn_size

        super(Decoder, self).__init__()
        self.embeddings = embeddings

        pad_id = embeddings.padding_idx
        if self.decoder_type == "transformer":
            self.transformer = nn.ModuleList(
                [onmt.modules.TransformerDecoder(self.hidden_size, opt, pad_id)
                 for _ in range(opt.dec_layers)])
        else:
            if self.input_feed:
                if opt.rnn_type == "LSTM":
                    stackedCell = onmt.modules.StackedLSTM
                else:
                    stackedCell = onmt.modules.StackedGRU
                self.rnn = stackedCell(opt.dec_layers, input_size,
                                       opt.rnn_size, opt.dropout)
            else:
                self.rnn = getattr(nn, opt.rnn_type)(
                     input_size, opt.rnn_size,
                     num_layers=opt.dec_layers,
                     dropout=opt.dropout
                )
            self.context_gate = None
            if opt.context_gate is not None:
                self.context_gate = ContextGateFactory(
                    opt.context_gate, input_size,
                    opt.rnn_size, opt.rnn_size,
                    opt.rnn_size
                )

        self.dropout = nn.Dropout(opt.dropout)

        # Std attention layer.
        self.attn = onmt.modules.GlobalAttention(
            opt.rnn_size,
            coverage=self._coverage,
            attn_type=opt.global_attention)

        # Separate Copy Attention.
        self._copy = False
        if opt.copy_attn:
            self.copy_attn = onmt.modules.GlobalAttention(
                opt.rnn_size, attn_type=opt.global_attention)
            self._copy = True

    def forward(self, input, src, context, state):
        """
        Forward through the decoder.

        Args:
            input (LongTensor):  (len x batch) -- Input tokens
            src (LongTensor)
            context:  (src_len x batch x rnn_size)  -- Memory bank
            state: an object initializing the decoder.

        Returns:
            outputs: (len x batch x rnn_size)
            final_states: an object of the same form as above
            attns: Dictionary of (src_len x batch)
        """
        # CHECKS
        t_len, n_batch = input.size()
        s_len, n_batch_, _ = src.size()
        s_len_, n_batch__, _ = context.size()
        aeq(n_batch, n_batch_, n_batch__)
        # aeq(s_len, s_len_)
        # END CHECKS
        if self.decoder_type == "transformer":
            if state.previous_input:
                input = torch.cat([state.previous_input.squeeze(2), input], 0)

        emb = self.embeddings(input.unsqueeze(2))

        # n.b. you can increase performance if you compute W_ih * x for all
        # iterations in parallel, but that's only possible if
        # self.input_feed=False
        outputs = []

        # Setup the different types of attention.
        attns = {"std": []}
        if self._copy:
            attns["copy"] = []
        if self._coverage:
            attns["coverage"] = []

        if self.decoder_type == "transformer":
            # Tranformer Decoder.
            assert isinstance(state, TransformerDecoderState)
            output = emb.transpose(0, 1).contiguous()
            src_context = context.transpose(0, 1).contiguous()
            for i in range(self.layers):
                output, attn \
                    = self.transformer[i](output, src_context,
                                          src[:, :, 0].transpose(0, 1),
                                          input.transpose(0, 1))
            outputs = output.transpose(0, 1).contiguous()
            if state.previous_input:
                outputs = outputs[state.previous_input.size(0):]
                attn = attn[:, state.previous_input.size(0):].squeeze()
                attn = torch.stack([attn])
            attns["std"] = attn
            if self._copy:
                attns["copy"] = attn
            state = TransformerDecoderState(input.unsqueeze(2))
        elif self.input_feed:
            assert isinstance(state, RNNDecoderState)
            output = state.input_feed.squeeze(0)
            hidden = state.hidden
            # CHECKS
            n_batch_, _ = output.size()
            aeq(n_batch, n_batch_)
            # END CHECKS

            coverage = state.coverage.squeeze(0) \
                if state.coverage is not None else None

            # Standard RNN decoder.
            for i, emb_t in enumerate(emb.split(1)):
                emb_t = emb_t.squeeze(0)
                if self.input_feed:
                    emb_t = torch.cat([emb_t, output], 1)

                rnn_output, hidden = self.rnn(emb_t, hidden)
                attn_output, attn = self.attn(rnn_output,
                                              context.transpose(0, 1))
                if self.context_gate is not None:
                    output = self.context_gate(
                        emb_t, rnn_output, attn_output
                    )
                    output = self.dropout(output)
                else:
                    output = self.dropout(attn_output)
                outputs += [output]
                attns["std"] += [attn]

                # COVERAGE
                if self._coverage:
                    coverage = coverage + attn \
                               if coverage is not None else attn
                    attns["coverage"] += [coverage]

                # COPY
                if self._copy:
                    _, copy_attn = self.copy_attn(output,
                                                  context.transpose(0, 1))
                    attns["copy"] += [copy_attn]
            state = RNNDecoderState(hidden, output.unsqueeze(0),
                                    coverage.unsqueeze(0)
                                    if coverage is not None else None)
            outputs = torch.stack(outputs)
            for k in attns:
                attns[k] = torch.stack(attns[k])
        else:
            assert isinstance(state, RNNDecoderState)
            assert emb.dim() == 3

            assert not self._coverage
            assert state.coverage is None

            # TODO: copy
            assert not self._copy

            hidden = state.hidden
            rnn_output, hidden = self.rnn(emb, hidden)

            # CHECKS
            t_len_, n_batch_, _ = rnn_output.size()
            aeq(n_batch, n_batch_)
            aeq(t_len, t_len_)
            # END CHECKS

            attn_outputs, attn_scores = self.attn(
                rnn_output.transpose(0, 1).contiguous(),    # (batch, t_len, d)
                context.transpose(0, 1)                     # (batch, s_len, d)
            )

            if self.context_gate is not None:
                outputs = self.context_gate(
                    emb.view(-1, emb.size(2)),
                    rnn_output.view(-1, rnn_output.size(2)),
                    attn_outputs.view(-1, attn_outputs.size(2))
                )
                outputs = outputs.view(t_len, n_batch, self.hidden_size)
                outputs = self.dropout(outputs)
            else:
                outputs = self.dropout(attn_outputs)        # (t_len, batch, d)
            state = RNNDecoderState(hidden, outputs[-1].unsqueeze(0), None)
            attns["std"] = attn_scores

        return outputs, state, attns


class NMTModel(nn.Module):
    def __init__(self, encoder, decoder, multigpu=False):
        self.multigpu = multigpu
        super(NMTModel, self).__init__()
        self.encoder = encoder
        self.decoder = decoder

    def _fix_enc_hidden(self, h):
        """
        The encoder hidden is  (layers*directions) x batch x dim
        We need to convert it to layers x batch x (directions*dim)
        """
        if self.encoder.num_directions == 2:
            h = torch.cat([h[0:h.size(0):2], h[1:h.size(0):2]], 2)
        return h

    def init_decoder_state(self, context, enc_hidden):
        if self.decoder.decoder_type == "transformer":
            return TransformerDecoderState()
        elif isinstance(enc_hidden, tuple):
            dec = RNNDecoderState(tuple([self._fix_enc_hidden(enc_hidden[i])
                                         for i in range(len(enc_hidden))]))
        else:
            dec = RNNDecoderState(self._fix_enc_hidden(enc_hidden))
        dec.init_input_feed(context, self.decoder.hidden_size)
        return dec

    def forward(self, src, tgt, lengths, dec_state=None):
        """
        Args:
            src, tgt, lengths
            dec_state: A decoder state object

        Returns:
            outputs (FloatTensor): (len x batch x rnn_size) -- Decoder outputs.
            attns (FloatTensor): Dictionary of (src_len x batch)
            dec_hidden (FloatTensor): tuple (1 x batch x rnn_size)
                                      Init hidden state
        """
        src = src
        tgt = tgt[:-1]  # exclude last target from inputs
        enc_hidden, context = self.encoder(src, lengths)
        enc_state = self.init_decoder_state(context, enc_hidden)
        out, dec_state, attns = self.decoder(tgt, src, context,
                                             enc_state if dec_state is None
                                             else dec_state)
        if self.multigpu:
            # Not yet supported on multi-gpu
            dec_state = None
            attns = None
        return out, attns, dec_state


class DecoderState(object):
    def detach(self):
        for h in self.all:
            if h is not None:
                h.detach_()

    def repeatBeam_(self, beamSize):
        self._resetAll([Variable(e.data.repeat(1, beamSize, 1))
                        for e in self.all])

    def beamUpdate_(self, idx, positions, beamSize):
        for e in self.all:
            a, br, d = e.size()
            sentStates = e.view(a, beamSize, br // beamSize, d)[:, :, idx]
            sentStates.data.copy_(
                sentStates.data.index_select(1, positions))


class RNNDecoderState(DecoderState):
    def __init__(self, rnnstate, input_feed=None, coverage=None):
        # all objects are X x batch x dim
        # or X x (beam * sent) for beam search
        if not isinstance(rnnstate, tuple):
            self.hidden = (rnnstate,)
        else:
            self.hidden = rnnstate
        self.input_feed = input_feed
        self.coverage = coverage
        self.all = self.hidden + (self.input_feed,)

    def init_input_feed(self, context, rnn_size):
        batch_size = context.size(1)
        h_size = (batch_size, rnn_size)
        self.input_feed = Variable(context.data.new(*h_size).zero_(),
                                   requires_grad=False).unsqueeze(0)
        self.all = self.hidden + (self.input_feed,)

    def _resetAll(self, all):
        vars = [Variable(a.data if isinstance(a, Variable) else a,
                         volatile=True) for a in all]
        self.hidden = tuple(vars[:-1])
        self.input_feed = vars[-1]
        self.all = self.hidden + (self.input_feed,)


class TransformerDecoderState(DecoderState):
    def __init__(self, input=None):
        # all objects are X x batch x dim
        # or X x (beam * sent) for beam search
        self.previous_input = input
        self.all = (self.previous_input,)

    def _resetAll(self, all):
        vars = [(Variable(a.data if isinstance(a, Variable) else a,
                          volatile=True))
                for a in all]
        self.previous_input = vars[0]
        self.all = (self.previous_input,)

    def repeatBeam_(self, beamSize):
        pass


def make_base_model(opt, model_opt, fields, checkpoint=None):
    """
    Args:
        opt: the option in current environment.
        model_opt: the option loaded from checkpoint.
        fields: `Field` objects for the model.
        checkpoint: the snapshot model.
    """
    # Make Encoder.
    src_vocab = fields["src"].vocab
    num_feat_embeddings = [len(feat_dict) for feat_dict in
                           ONMTDataset.collect_feature_dicts(fields)]
    embeddings = build_embeddings(
                model_opt, src_vocab.stoi[onmt.IO.PAD_WORD],
                len(src_vocab), for_encoder=True,
                num_feat_embeddings=num_feat_embeddings)

    if model_opt.model_type == "text":
        encoder = Encoder(model_opt.encoder_type, model_opt.brnn,
                          model_opt.rnn_type, model_opt.enc_layers,
                          model_opt.rnn_size, model_opt.dropout,
                          embeddings)
    elif model_opt.model_type == "img":
        encoder = onmt.modules.ImageEncoder(model_opt.layers,
                                            model_opt.brnn,
                                            model_opt.rnn_size,
                                            model_opt.dropout)
    else:
        assert False, ("Unsupported model type %s"
                       % (model_opt.model_type))

    # Make Decoder.
    tgt_vocab = fields["tgt"].vocab
    embeddings = build_embeddings(
                    model_opt, tgt_vocab.stoi[onmt.IO.PAD_WORD],
                    len(tgt_vocab), for_encoder=False)
    decoder = onmt.Models.Decoder(model_opt, embeddings)

    # Make NMTModel(= Encoder + Decoder).
    model = onmt.Models.NMTModel(encoder, decoder)

    # Make Generator.
    if not model_opt.copy_attn:
        generator = nn.Sequential(
            nn.Linear(model_opt.rnn_size, len(fields["tgt"].vocab)),
            nn.LogSoftmax())
        if model_opt.share_decoder_embeddings:
            generator[0].weight = decoder.embeddings.word_lut.weight
    else:
        generator = onmt.modules.CopyGenerator(model_opt, fields["src"].vocab,
                                               fields["tgt"].vocab)

    if checkpoint is not None:
        print('Loading model')
        model.load_state_dict(checkpoint['model'])
        generator.load_state_dict(checkpoint['generator'])

    if hasattr(opt, 'gpuid'):
        cuda = len(opt.gpuid) >= 1
    elif hasattr(opt, 'gpu'):
        cuda = opt.gpu > -1
    else:
        cuda = False

    if cuda:
        model.cuda()
        generator.cuda()
    else:
        model.cpu()
        generator.cpu()
    model.generator = generator
    return model

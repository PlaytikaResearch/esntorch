# The MIT License (MIT)
#
# Copyright (c) 2021 Playtika Ltd.
#
# Permission is hereby granted, free of charge, to any person obtaining a copy of this software
# and associated documentation files (the "Software"), to deal in the Software without restriction,
# including without limitation the rights to use, copy, modify, merge, publish, distribute, sublicense,
# and/or sell copies of the Software, and to permit persons to whom the Software is furnished to do so,
# subject to the following conditions:
#
# The above copyright notice and this permission notice shall be included in all copies
# or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR IMPLIED,
# INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT.
# IN NO EVENT SHALL THE AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM,
# DAMAGES OR OTHER LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE,
# ARISING FROM, OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE SOFTWARE.

import torch
import torch.nn as nn
import esntorch.utils.matrix as mat
import esntorch.core.reservoir as res
import esntorch.core.learning_algo as la
import esntorch.core.merging_strategy as ms
from tqdm.notebook import tqdm_notebook


class EchoStateNetwork(nn.Module):
    """
    Implements the Echo State Network (ESN) per se.
    An ESN consists of the combination of a layer, a merging strategy and a learning algorithm.

    Parameters
    ----------
    embedding_weights : torch.Tensor
        Embedding matrix.
    distribution : str
        Distribution of the layer: 'uniform' or 'gaussian'
    input_dim : int
        Input dimension.
    reservoir_dim : int
        Reservoir dimension.
    bias_scaling : float
        Bias scaling: bounds used for the bias random generation.
    sparsity : float
        Sparsity of the layer ((between 0 and 1))
    spectral_radius : float
        Spectral radius of the layer weights.
        Should theoretically be below 1, but slightly above 1 works in practice.
    leaking_rate : float (between 0 and 1)
        Leaking rate of teh layer (between 0 and 1).
        Determines the amount of last state and current input involved in the current state updating.
    activation_function : str
        Activation function of the layer cells ('tanh' by default).
    input_scaling : float
        Input scaling: bounds used for the input weights random generation (if distribution == 'uniform').
    mean : float
        Mean of the input and layer weights (if distribution == 'gaussian')
    std : float
        Standard deviation of the input and layer weights (if distribution == 'gaussian')
    learning_algo : src.models.learning_algo.RidgeRegression, src.models.learning_algo.LogisticRegression
        Learning algorithm used to learn the targets from the layer (merged) states.
    criterion : torch.nn.modules.loss
        Criterion used to compute the loss between tagets and predictions (only if leaning_algo ≠ RidgeRegression).
    optimizer : torch.optim
        Optimizer used in the gradient descent method (only if leaning_algo ≠ RidgeRegression).
    merging_strategy : src.models.merging_strategy.MergingStrategy
        Merging strategy used to merge the sucessive layer states.
    bidirectional : bool
        Flag for bi-directionality.
    mode : str
        The ESN can be used in different modes in order to implement kinds of models
        (classical ESN, baselines, etc.):
        If mode=='recurrent_layer', the classical ESN is implemented (EMB + RESERVOIR + LA).
        If mode=='linear_layer', the Custom Baseline is implemented (EMB + LINEAR_LAYER + LA).
        If mode=='no_layer', the Simple Baseline is implemented (EMB + LA).
    deep : bool
        If true implements the Deep Echo State Network (BS) per se (EMB + multiple RESERVOIR + LA).
    seed : torch._C.Generator
        Random seed.
    """

    # Constructor
    def __init__(self,
                 learning_algo=None,
                 criterion=None,
                 optimizer=None,
                 merging_strategy=None,
                 bidirectional=False,
                 lexicon=None,
                 deep=False,
                 device=torch.device('cpu'),
                 **kwargs
                 ):

        super().__init__()
        self.device = device

        if deep:
            self.layer = res.DeepLayer(device=device, **kwargs)
        else:
            self.layer = res.create_layer(device=device, **kwargs)

        self.merging_strategy = ms.MergingStrategy(merging_strategy, lexicon=lexicon)

        self.learning_algo = learning_algo
        self.criterion = criterion
        self.optimizer = optimizer
        self.bidirectional = bidirectional

    def warm_up(self, warm_up_sequence):
        """
        Passes a warm up sequence to the ESN and set the warm state as the new initial state.

        Parameters
        ----------
        warm_up_sequence : torch.Tensor
            1D tensor: word indices of the warm up sentence.
        """

        self.layer.warm_up(warm_up_sequence)

    def _apply_pooling_strategy(self, states, lengths, texts,
                                reversed_states=None, additional_fts=None):
        """
        Merges the corresponding layer states depending on the merging strategy
        and the whether to apply bi-directionality or not.

        Parameters
        ----------
        states : torch.Tensor
            3D tensor: the states of the token that went through the layer
        lengths : torch.Tensor
            1D tensor, the token sentences true lengths.
        texts: torch.Tensor
            2D tensor containing word indices of the texts in the batch.
        reversed_states : torch.Tensor
            3D tensor: Optional, the states of the token that went through the layer
            in the reverse order when the bi-directional flag is set.
        additional_fts : None, torch.Tensor
            2D tensor containing new features (e.g. tf-idf)
            to be concatenated to each merged state (batch size x dim additional_fts).

        Returns
        -------
        final_states : torch.Tensor
            2D tensor : the merged tensors.
        """
        if self.bidirectional:
            if self.merging_strategy.merging_strategy is None:
                # concatenate the normal and reversed states along the layer dimension while not
                # forgetting to first put the reversed words in the correct order, then apply the merging.
                restored_states = reversed_states.clone()
                for i, l in enumerate(lengths):
                    restored_states[i, :l] = torch.flip(restored_states[i, :l], [0])

                concatenated_states = torch.cat([states, restored_states], dim=2)
                final_states = self.merging_strategy(concatenated_states, lengths, texts)
            else:
                # concatenate the normal and reversed states after their merging
                normal_merged_states = self.merging_strategy(states, lengths, texts, additional_fts)
                reversed_merged_states = self.merging_strategy(reversed_states, lengths, texts)
                # concatenate the batches across features dimension
                final_states = torch.cat([normal_merged_states, reversed_merged_states], dim=1)
        else:
            final_states = self.merging_strategy(states, lengths, texts, additional_fts)

        return final_states

    def _fit_direct(self, train_dataloader):
        """
        Fits the ESN using the Ridge regression closed-form solution.

        Parameters
        ----------
        train_dataloader: torchtext.data.iterator.Iterator
            Training dataset.

        Returns
        -------
        None
        """

        print("Computing closed-form solution...")

        states_l = []
        labels_l = []

        # loop over batches
        for i, batch in enumerate(tqdm_notebook(train_dataloader)):

            if callable(self.layer.embedding):  # HuggingFace
                batch_text = batch
                batch_label = batch["labels"].to(self.device)
                if 'additional_fts' in batch.keys():
                    additional_fts = batch["additional_fts"].to(self.device)
                else:
                    additional_fts = None
            else:  # TorchText
                batch_text = batch.text
                batch_label = batch.label

            # Pass the tokens through the layer
            states, lengths = self.layer.forward(batch_text)  # states

            # Do the same as above but with the sentences reversed
            reversed_states = None
            if self.bidirectional:
                reversed_states, _ = self.layer.reverse_forward(batch_text)

            labels = batch_label

            # if merging_strategy is None: duplicate labels
            if self.merging_strategy.merging_strategy is None:
                labels = mat.duplicate_labels(labels, lengths)

            # apply the correct merging strategy and bi-directionality if needed.
            final_states = self._apply_pooling_strategy(states, lengths, batch_text,
                                                        reversed_states, additional_fts)
            states_l.append(final_states)
            labels_l.append(labels)

        all_states, all_labels = torch.cat(states_l, dim=0), torch.cat(labels_l, dim=0)

        self.learning_algo.fit(all_states, all_labels)

        print("\nTraining complete.")

        return None

    def _fit_GD(self, train_dataloader, epochs=1, iter_steps=100):
        """
        Fits the ESN using gradient descent.

        Parameters
        ----------
        train_dataloader: torchtext.data.iterator.Iterator
            Training dataset.
        epochs: int
            Number of epochs.
        iter_steps: int
            Number of iteration steps before displaying the loss.

        Returns
        -------
        loss_l: list
            List of losses.
        """

        print("Performing gradient descent...")

        loss_l = []
        n_iter = 0

        # loop over epochs
        for epoch in tqdm_notebook(range(int(epochs))):

            # loop over batches
            for i_batch, batch in enumerate(tqdm_notebook(train_dataloader, leave=True)):

                if callable(self.layer.embedding):  # HuggingFace
                    batch_text = batch
                    batch_label = batch["labels"].to(self.device)
                    if 'additional_fts' in batch.keys():
                        additional_fts = batch["additional_fts"].to(self.device)
                    else:
                        additional_fts = None
                else:  # TorchText
                    batch_text = batch.text
                    batch_label = batch.label

                # Pass the tokens through the layer
                states, lengths = self.layer.forward(batch_text)  # states

                # Do the same as above but with the sentences reversed
                reversed_states = None
                if self.bidirectional:
                    reversed_states, _ = self.layer.reverse_forward(batch_text)

                labels = batch_label.type(torch.int64)  # labels (converted to int for the loss)
                # if merging_strategy is None: duplicate labels
                if self.merging_strategy.merging_strategy is None:
                    labels = mat.duplicate_labels(labels, lengths)

                # apply the correct merging strategy and bi-directionality if needed.
                final_states = self._apply_pooling_strategy(states, lengths, batch_text,
                                                            reversed_states, additional_fts)

                outputs = self.learning_algo(final_states)  # outputs

                if isinstance(self.criterion, torch.nn.MultiLabelSoftMarginLoss) or \
                        isinstance(self.criterion, torch.nn.BCEWithLogitsLoss):
                    labels = torch.nn.functional.one_hot(labels).double()

                loss = self.criterion(outputs, labels)  # compute loss
                self.optimizer.zero_grad()  # reset optimizer gradient
                loss.backward()  # backward pass
                self.optimizer.step()  # gradient update

                n_iter += 1

                if n_iter % iter_steps == 0:
                    print("Iteration: {iteration} Loss: {loss}".format(iteration=n_iter, loss=loss.item()))
                    loss_l.append(loss.item())

        print("\nTraining complete.")

        return loss_l

    def fit(self, train_dataloader, epochs=1, iter_steps=100):
        """
        Fits the ESN on the training set. Fitting is performed according to:
        1) a learning algorithm;
        2) a merging strategy.

        Parameters
        ----------
        train_dataloader : torchtext.data.iterator.Iterator
            Training dataset.
        epochs : int
            Number of traning epochs (only if leaning_algo ≠ RidgeRegression)
        iter_steps : int
            Number of traning steps after which loss is recorded (only if leaning_algo ≠ RidgeRegression).

        Returns
        -------
        loss_l : None, list
            None if closed-form solution used.
            list of losses if gradient descent used.
        """

        # Closed-form training (for RR, RF)
        if isinstance(self.learning_algo, la.RidgeRegression) or \
                isinstance(self.learning_algo, la.RidgeRegression_skl) or \
                isinstance(self.learning_algo, la.LogisticRegression_skl) or \
                isinstance(self.learning_algo, la.LinearSVC):

            return self._fit_direct(train_dataloader)

        # Gradient descent training (for LR or deep NN)
        else:

            return self._fit_GD(train_dataloader, epochs, iter_steps)

    def _compute_predictions(self, states, lengths):
        """
        Takes layer states, passes them to the learning algorithm and computes predictions out of them.
        Predictions are computed differently depending on whether the merging strategy is None or not.
        If merging strategy is None, the predictions are computed as follows:
        For each input sentence u:
        (1) the corresponding layer states X_u are passed through the learning algorithm;
        (2) the raw outputs of the algorithm Y_u are then averaged row-wise, yielding a 1-dim tensor y_u;
        (3) the prediction is arg_max(y_u).
        If merging strategy is not None, the predictions are computed as follows:
        For each input sentence u:
        (1) the corresponding merged layer state x_u is passed through the learning algorithm,
        yielding a 1-dim tensor y_u;
        (3) the prediction is the arg_max(y_u).

        Parameters
        ----------
        states: torch.Tensor
            Reservoir states obtained after processing the inputs.
        lengths: torch.Tensor
            Lengths of input texts in the batch.

        Returns
        -------
        predictions: torch.Tensor
            Predictions computed from the outputs.
        """

        raw_outputs = self.learning_algo(states)

        if self.merging_strategy.merging_strategy is None:  # merging strategy is None

            # tmp = list(lengths.numpy())
            # tmp = [sum(tmp[:i]) - 1 for i in range(1, len(tmp) + 1)]
            # predictions = outputs[tmp].type(torch.int64)

            tmp = list(lengths.cpu().numpy())
            tmp = [0] + [sum(tmp[:i]) for i in range(1, len(tmp) + 1)]
            outputs = torch.stack([torch.mean(raw_outputs[tmp[i]:tmp[i + 1]], dim=0) for i in range(len(tmp) - 1)])
            predictions = outputs.argmax(dim=1)

        else:  # merging strategy is not None
            if raw_outputs.dim() != 1:  # the learning algo returns the probas
                outputs = raw_outputs.argmax(dim=1).float()
            else:  # the learning algo returns the classes
                outputs = raw_outputs.float()
            predictions = outputs.type(torch.int64)

        return predictions

    def predict(self, dataloader, verbose=True):
        """
        Evaluates the ESN on a dataset (train or test).
        Returns the list of prediction labels. If true labels are known, returns the accuracy also.

        Parameters
        ----------
        dataloader : torchtext.data.iterator.Iterator
            Test dataset.

        Returns
        -------
        predictions_l, accuracy : list, float
            List of prediction labels and accuracy.
            If true labels are not known, returns None for accuracy.
        """

        predictions_l = []
        correct = 0
        total = 0
        testing_mode = False

        for i, batch in enumerate(tqdm_notebook(dataloader)):

            if callable(self.layer.embedding):  # HuggingFace
                batch_text = batch
                batch_label = batch["labels"].to(self.device)
                if 'additional_fts' in batch.keys():
                    additional_fts = batch["additional_fts"].to(self.device)
                else:
                    additional_fts = None
            else:  # TorchText
                batch_text = batch.text
                batch_label = batch.label

            # Pass the tokens through the layer
            states, lengths = self.layer.forward(batch_text)

            # Do the same as above but with the sentences reversed
            reversed_states = None
            if self.bidirectional:
                reversed_states, _ = self.layer.reverse_forward(batch_text)

            # apply the correct merging strategy and bi-directionality if needed.
            final_states = self._apply_pooling_strategy(states, lengths, batch_text,
                                                        reversed_states, additional_fts)

            predictions = self._compute_predictions(final_states, lengths)
            predictions_l.append(predictions.reshape(-1))

            # if labels available, compute accuracy
            try:
                labels = batch_label.type(torch.int64)
                total += labels.size(0)
                correct += (predictions == labels).sum()
                testing_mode = True
            # otherwise: pure prediction mode
            except Exception:
                pass

        accuracy = 100 * correct.item() / float(total) if testing_mode else None
        predictions_l = torch.cat(predictions_l, dim=0).cpu().detach().numpy()

        if verbose and testing_mode:
            print("\nAccuracy: {}.".format(accuracy))

        return predictions_l, accuracy

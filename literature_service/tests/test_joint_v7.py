import pytest


def test_mil_does_not_multiply_positive_labels_over_witnesses():
    torch=pytest.importorskip('torch')
    torch.set_num_threads(1)
    from nutri_literature.joint_v7 import bag_pool,biored_loss
    logits=torch.tensor([[1.,-1.],[1.,-1.]],requires_grad=True)
    assert torch.allclose(bag_pool(logits),logits[0])


def test_low_rank_updates_leave_base_weights_frozen():
    torch=pytest.importorskip('torch')
    torch.set_num_threads(1)
    transformers=pytest.importorskip('transformers')
    from nutri_literature.encoder_v7 import install_lora
    cfg=transformers.BertConfig(hidden_size=24,num_hidden_layers=12,num_attention_heads=4,intermediate_size=32)
    model=transformers.BertModel(cfg).eval()
    inputs={'input_ids':torch.tensor([[1,2,3,4]])}
    before=model(**inputs).last_hidden_state.detach()
    install_lora(model)
    model.eval()
    after=model(**inputs).last_hidden_state
    assert torch.equal(before,after.detach())
    after.square().sum().backward()
    trainable=[n for n,p in model.named_parameters() if p.requires_grad]
    assert len(trainable)==16 and all(n.endswith(('.a','.b')) for n in trainable)
    assert all(p.grad is None for n,p in model.named_parameters() if not p.requires_grad)

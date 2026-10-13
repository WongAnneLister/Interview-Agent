# live 测试

这里放会调用真实厂商（百炼、DeepSeek 等）的测试，每个测试都要加 `@pytest.mark.live`。

- 默认不运行，CI 里也不运行；手动验证时执行 `make test-live`。
- 需要的录音样本放在 `fixtures/`，输出放在 `out/`，两者都不提交到仓库。

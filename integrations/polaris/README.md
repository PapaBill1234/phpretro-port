# Strict Polaris SSO consumer

Apply the patch to pinned commit `0101ce955069af1bcf1dd4299b03250679d9b1e1`. It validates unmodified printable ASCII tickets and atomically consumes only unexpired tickets before identity loading. Recovery remains an independent credential.

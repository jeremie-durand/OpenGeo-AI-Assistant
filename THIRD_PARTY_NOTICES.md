# Third-Party Notices

The backend Docker image bundles the following third-party material in addition to
the Python packages listed in `opengeo-ai-assistant/requirements.txt`.

## paraphrase-multilingual-MiniLM-L12-v2

- **Model:** `sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2`
  (<https://huggingface.co/sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2>)
- **Distributed as:** Qdrant's ONNX export of that model,
  `Qdrant/paraphrase-multilingual-MiniLM-L12-v2-onnx-Q`
  (<https://huggingface.co/Qdrant/paraphrase-multilingual-MiniLM-L12-v2-onnx-Q>),
  revision `faf4aa4225822f3bc6376869cb1164e8e3feedd0`, as downloaded by fastembed.
  The image build fails if a different revision is downloaded. This project does not
  modify the model further.
- **Licence:** Apache License 2.0
- **Licence file in the image:**
  `/opt/models/paraphrase-multilingual-MiniLM-L12-v2-LICENSE` (source:
  `opengeo-ai-assistant/licenses/paraphrase-multilingual-MiniLM-L12-v2-LICENSE`).
  Neither upstream repository ships a LICENSE or NOTICE file, so the licence text is
  vendored here.
- **Used for:** text embeddings for vector search (`EMBEDDING_PROVIDER=local`).

# RFQ Review and Quote Creation Agent

## Overview
RFQ Review and Quote Creation Agent is a professional customer_service agent designed for text interactions.

## Features


## Installation

1. Install dependencies:
```bash
pip install -r requirements.txt
```

2. Set up environment variables:
```bash
cp .env.example .env
# Edit .env with your API keys
```

3. Run the agent:
```bash
python agent.py
```

## Configuration

The agent uses the following environment variables:
- `OPENAI_API_KEY`: OpenAI API key
- `ANTHROPIC_API_KEY`: Anthropic API key (if using Anthropic)
- `GOOGLE_API_KEY`: Google API key (if using Google)

## Usage

```python
from agent import RFQ Review and Quote Creation AgentAgent

agent = RFQ Review and Quote Creation AgentAgent()
response = await agent.process_message("Hello!")
```

## Domain: customer_service
## Personality: professional
## Modality: text
from dataclasses import dataclass


@dataclass(frozen=True)
class Workflow:
    key: str
    page_path: str
    category: str
    tool_used: str
    checkpoint: str | None = None
    rewrites_prompt: bool = False


IMAGE = Workflow('image', '/ai/image-designer-v6', 'AI Image Designer v6', 'AI Image Designer v6')
CHARACTER = Workflow('character', '/ai/ai-design-assistants', 'Character Creator',
                     'AI Design Assistants', checkpoint='character_creator', rewrites_prompt=True)

# Image agent failures

Check image reading, answer generation and conversation saving separately. A generated answer can still fail to save. Supported image input is PNG, JPEG or WebP with at most 5 MB of image data; verify the vision model connection and image support. AGENT_RESPONSE_SAVE_FAILED identifies answer persistence; AGENT_IMAGE_PROCESSING_FAILED identifies image processing. Record the model, role and error. Database compatibility repairs apply at updated app startup.

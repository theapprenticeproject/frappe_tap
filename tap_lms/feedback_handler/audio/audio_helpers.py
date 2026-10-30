import frappe
import os
from elevenlabs import ElevenLabs


def get_elevenlabs_client() -> ElevenLabs:
	"""
	Fetch ElevenLabs client using API key stored in ElevenLabs Settings.
	"""
	settings = frappe.get_single("ElevenLabs Settings")

	if not settings.enabled:
		frappe.throw("ElevenLabs TTS is not enabled. Enable it in ElevenLabs Settings.")

	api_key = settings.get_password("api_key")
	if not api_key:
		frappe.throw("ElevenLabs API key is not configured. Add it in ElevenLabs Settings.")

	return ElevenLabs(api_key=api_key)


def generate_speech_file(text: str, langcode: str, output_path: str, tone: str = None) -> None:
	"""
	Generate speech from text using ElevenLabs TTS and save to file.
	
	Args:
		text: Text to convert to speech
		langcode: Language code (hi, pa, mr)
		output_path: Full path where the audio file should be saved
		tone: Optional tone parameter (currently not used by ElevenLabs API)
	
	Returns:
		None (saves file to output_path)
	"""
	# Voice mapping - add Hindi voice ID if available
	voices = {
		"mr": "Y8AULI9gwzMzFX41r7IP",  # Misha
		"pa": "Y8AULI9gwzMzFX41r7IP",  # Misha
		"hi": "Y8AULI9gwzMzFX41r7IP",  # Misha
		"en": "Y8AULI9gwzMzFX41r7IP",  # Misha
		"en": "Y8AULI9gwzMzFX41r7IP",  # Misha
		"ka": "Y8AULI9gwzMzFX41r7IP",  # Misha
	}
	
	# Get voice ID, default to Marathi if not found
	lang_voice_id = voices.get(langcode, voices["hi"])
	
	frappe.logger().info(f"Calling TTS model for voice output (langcode: {langcode}, voice_id: {lang_voice_id})")
	
	# Call ElevenLabs API
	labs_client = get_elevenlabs_client()
	response = labs_client.text_to_speech.convert(
		text=text,
		voice_id=lang_voice_id,
		model_id="eleven_v3",
	) ## default mp3_44100_128 format
	
	frappe.logger().info(f"Response received from TTS model, saving to: {output_path}")
	print("#"*30)
	print(output_path)
	
	# Ensure output directory exists
	output_dir = os.path.dirname(output_path)
	if output_dir and not os.path.exists(output_dir):
		os.makedirs(output_dir, exist_ok=True)
	
	# Write audio chunks to file
	with open(output_path, "wb") as f:
		for chunk in response:
			if chunk:
				f.write(chunk)
	
	frappe.logger().info(f"Audio file generated successfully: {output_path}")

 from fastapi import FastAPI, Form                                                                                                            
  from fastapi.responses import PlainTextResponse                                                                                              
  import anthropic
  import os                                                                                                                                    
  from twilio.twiml.messaging_response import MessagingResponse
                                                                                                                                               
  app = FastAPI()
  client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])                                                                        
  conversations = {}

  @app.post("/webhook")                                                                                                                        
  async def webhook(From: str = Form(...), Body: str = Form(...)):
      if From not in conversations:                                                                                                            
          conversations[From] = []
      conversations[From].append({"role": "user", "content": Body})
      if len(conversations[From]) > 20:
          conversations[From] = conversations[From][-20:]                                                                                      
      response = client.messages.create(
          model="claude-sonnet-4-6",                                                                                                           
          max_tokens=1024,                                                                                                                     
          system="אתה סוכן אישי מועיל. ענה בקצרה.",
          messages=conversations[From]                                                                                                         
      )           
      reply = response.content[0].text                                                                                                         
      conversations[From].append({"role": "assistant", "content": reply})
      resp = MessagingResponse()                                                                                                               
      resp.message(reply)
      return PlainTextResponse(str(resp), media_type="application/xml")                                                                        
                                                                                                                                               
  @app.get("/")
  async def health():                                                                                                                          
      return {"status": "ok"}

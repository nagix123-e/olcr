import {afterEach,describe,it,expect,vi} from "vitest";
vi.mock("./native",()=>({native:vi.fn()}));
import {native} from "./native";
import {isGuiGroundingIntent,isTextOnlyReadIntent,chooseOcrGroundedTextTarget,prepareGuiGroundingOcr,isMedicationImageIntent} from "./ocr";

afterEach(()=>{vi.useRealTimers();vi.restoreAllMocks();});

describe("explicit OCR intent",()=>{
  it.each(["Read the text in this image","Extract the labels from the screenshot","What does this say?","この画像の文字を読んで","文字起こししてください","テキストを抽出して"])("accepts text-only request: %s",text=>expect(isTextOnlyReadIntent(text)).toBe(true));
  it.each(["Click Save in this image","Where is the button?","画像の保存ボタンを押して","Describe this screenshot","画像を確認してください"])("leaves perception/action request on VLM: %s",text=>expect(isTextOnlyReadIntent(text)).toBe(false));
});

describe("OCR-seeded GUI ROI selection",()=>{
  const save={text:"Save",bboxNormalized:[0.4,0.5,0.6,0.6],confidence:0.98};
  it.each(["Click Save in this screenshot","このスクリーンショットの保存ボタンを押して"]) (
    "recognizes GUI grounding intent: %s",text=>expect(isGuiGroundingIntent(text)).toBe(true));
  it("chooses a unique OCR label that matches the request",()=>{
    expect(chooseOcrGroundedTextTarget("Click Save in this screenshot",[save,{text:"Cancel",bboxNormalized:[0.7,0.5,0.9,0.6],confidence:0.99}])).toEqual(save);
    expect(chooseOcrGroundedTextTarget("このスクリーンショットの保存ボタンを押して",[{text:"保存",bboxNormalized:[0.4,0.5,0.6,0.6],confidence:0.98}])?.text).toBe("保存");
  });
  it("does not pick a tie or low-confidence OCR label",()=>{
    expect(chooseOcrGroundedTextTarget("Click Save",[save,{...save,bboxNormalized:[0.7,0.5,0.9,0.6]}])).toBeNull();
    expect(chooseOcrGroundedTextTarget("Click Save",[{...save,confidence:0.4}])).toBeNull();
  });
  it("returns hash-bound duration telemetry when native OCR fails without exposing OCR content",async()=>{
    const invoke=vi.fn().mockRejectedValue(new Error("native OCR detail"));
    vi.mocked(native).mockReturnValue({core:{invoke}} as never);
    vi.spyOn(performance,"now").mockReturnValueOnce(12).mockReturnValueOnce(19);
    const imageSha256="01".repeat(32);
    await expect(prepareGuiGroundingOcr("Find the Save button","data:image/png;base64,eA==",imageSha256))
      .resolves.toEqual({ocr_attempt:{source:"APPLE_VISION_OCR",status:"error",duration_ms:7,image_sha256:imageSha256}});
    expect(invoke).toHaveBeenCalledWith("recognize_image_text",{dataUrl:"data:image/png;base64,eA=="});
  });
  it("bounds a hung native OCR call, returns a hash-bound timeout receipt, and avoids piling up workers",async()=>{
    vi.useFakeTimers();
    let finishNativeCall:(value:{source:string;observationId:string;coordinateSpace:string;width:number;height:number;elements:never[]})=>void=()=>{};
    const invoke=vi.fn(()=>new Promise(resolve=>{finishNativeCall=resolve;}));
    vi.mocked(native).mockReturnValue({core:{invoke}} as never);
    const imageSha256="ab".repeat(32);
    const pending=prepareGuiGroundingOcr("Find the Save button","data:image/png;base64,eA==",imageSha256);
    await vi.advanceTimersByTimeAsync(1500);
    await expect(pending).resolves.toEqual({ocr_attempt:{source:"APPLE_VISION_OCR",status:"error",duration_ms:1500,image_sha256:imageSha256}});
    await expect(prepareGuiGroundingOcr("Find the Save button","data:image/png;base64,eA==",imageSha256)).resolves.toBeNull();
    expect(invoke).toHaveBeenCalledTimes(1);
    finishNativeCall({source:"APPLE_VISION_OCR",observationId:"late",coordinateSpace:"normalized_image_top_left",width:10,height:10,elements:[]});
    await Promise.resolve();
  });
  it("accepts cold-start OCR that completes after the attachment timeout within the grounding budget",async()=>{
    vi.useFakeTimers();
    const observation={source:"APPLE_VISION_OCR",observationId:"cold",coordinateSpace:"normalized_image_top_left",width:10,height:10,elements:[]};
    const invoke=vi.fn(()=>new Promise(resolve=>setTimeout(()=>resolve(observation),1000)));
    vi.mocked(native).mockReturnValue({core:{invoke}} as never);
    const imageSha256="cd".repeat(32);
    const pending=prepareGuiGroundingOcr("Save","data:image/png;base64,eA==",imageSha256);
    await vi.advanceTimersByTimeAsync(1000);
    await expect(pending).resolves.toMatchObject({ocr_observation:{...observation,imageSha256},ocr_duration_ms:1000});
  });
});

describe("medication image intent",()=>{
  it("recognizes prescription scheduling without classifying format conversion as medication management",()=>{
    expect(isMedicationImageIntent("この処方箋を服用予定にして")).toBe(true);
    expect(isMedicationImageIntent("この処方箋をPDFに変換して")).toBe(false);
  });
});

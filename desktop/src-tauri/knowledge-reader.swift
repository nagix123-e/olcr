import Foundation
import PDFKit
import Vision
import ImageIO
import AppKit

func ocr(_ image: CGImage) throws -> String {
    guard image.width <= 16384, image.height <= 16384,
          image.width * image.height <= 50_000_000 else { throw Failure.invalid }
    if image.width < 32 || image.height < 32 { return "" }
    let request = VNRecognizeTextRequest()
    request.recognitionLevel = .accurate
    request.recognitionLanguages = ["ja-JP", "en-US"]
    request.usesLanguageCorrection = true
    try VNImageRequestHandler(cgImage: image).perform([request])
    return (request.results ?? []).compactMap { $0.topCandidates(1).first?.string }.joined(separator: "\n")
}
enum Failure: Error { case invalid }
do {
    let input = FileHandle.standardInput.readDataToEndOfFile()
    guard input.count < 14_000_000,
          let value = try JSONSerialization.jsonObject(with: input) as? [String: String],
          let encoded = value["data_base64"], let data = Data(base64Encoded: encoded),
          data.count <= 10_000_000 else { throw Failure.invalid }
    var content = ""
    if (value["name"] ?? "").lowercased().hasSuffix(".pdf") {
        guard let document = PDFDocument(data: data), !document.isLocked,
              document.pageCount > 0, document.pageCount <= 100 else { throw Failure.invalid }
        for index in 0..<document.pageCount {
            guard let page = document.page(at: index) else { throw Failure.invalid }
            var text = page.string ?? ""
            if text.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty {
                let image = page.thumbnail(of: NSSize(width: 2000, height: 2800), for: .mediaBox)
                guard let cg = image.cgImage(forProposedRect: nil, context: nil, hints: nil) else { throw Failure.invalid }
                text = try ocr(cg)
            }
            content += "[Page \(index + 1)]\n\(text)\n"
            guard content.count <= 100_000 else { throw Failure.invalid }
        }
    } else {
        guard let source = CGImageSourceCreateWithData(data as CFData, nil),
              let properties = CGImageSourceCopyPropertiesAtIndex(source, 0, nil) as? [CFString: Any],
              let width = properties[kCGImagePropertyPixelWidth] as? NSNumber,
              let height = properties[kCGImagePropertyPixelHeight] as? NSNumber,
              width.intValue > 0, height.intValue > 0,
              width.intValue <= 16384, height.intValue <= 16384,
              width.intValue * height.intValue <= 50_000_000,
              let image = CGImageSourceCreateImageAtIndex(source, 0, nil) else { throw Failure.invalid }
        content = try ocr(image)
    }
    guard content.count <= 100_000 else { throw Failure.invalid }
    // Empty OCR is explicit reference metadata; the original file remains intact.
    let output = try JSONSerialization.data(withJSONObject: ["content": content])
    FileHandle.standardOutput.write(output)
} catch {
    FileHandle.standardError.write(Data("Knowledge reader: \(error)\n".utf8))
    FileHandle.standardOutput.write(Data("{\"error\":\"AGENT_KNOWLEDGE_DOCUMENT_INVALID_OR_TOO_LARGE\"}".utf8))
    exit(1)
}

import Foundation
import Vision
let input=URL(fileURLWithPath:CommandLine.arguments[1])
let request=VNDetectBarcodesRequest()
try VNImageRequestHandler(url:input,options:[:]).perform([request])
let values=(request.results ?? []).compactMap { $0.payloadStringValue }
let data=try JSONSerialization.data(withJSONObject:values)
print(String(data:data,encoding:.utf8)!)

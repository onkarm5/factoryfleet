package io.factoryfleet.backend.asset;

import static org.hamcrest.Matchers.hasSize;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.get;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.post;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.jsonPath;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.status;

import org.junit.jupiter.api.Test;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.boot.test.autoconfigure.web.servlet.AutoConfigureMockMvc;
import org.springframework.boot.test.context.SpringBootTest;
import org.springframework.http.MediaType;
import org.springframework.test.web.servlet.MockMvc;

@SpringBootTest
@AutoConfigureMockMvc
class AssetControllerTest {

    @Autowired
    private MockMvc mockMvc;

    @Test
    void registersAMachineAndListsItForItsSite() throws Exception {
        mockMvc.perform(post("/api/v1/sites/PLANT-A/assets")
                        .contentType(MediaType.APPLICATION_JSON)
                        .content("""
                                {
                                  "assetId": "PRESS-01",
                                  "machineType": "HYDRAULIC_PRESS",
                                  "firmwareVersion": "2.4.1",
                                  "sensors": ["vibration", "temperature"]
                                }
                                """))
                .andExpect(status().isOk())
                .andExpect(jsonPath("$.siteId").value("PLANT-A"))
                .andExpect(jsonPath("$.assetId").value("PRESS-01"))
                .andExpect(jsonPath("$.health").value("UNKNOWN"))
                .andExpect(jsonPath("$.sensors", hasSize(2)));

        mockMvc.perform(get("/api/v1/sites/PLANT-A/assets"))
                .andExpect(status().isOk())
                .andExpect(jsonPath("$[0].assetId").value("PRESS-01"));
    }

    @Test
    void returnsNotFoundForAnUnregisteredAsset() throws Exception {
        mockMvc.perform(get("/api/v1/sites/PLANT-A/assets/GHOST-01"))
                .andExpect(status().isNotFound());
    }

    @Test
    void rejectsARegistrationDeclaringNoSensors() throws Exception {
        mockMvc.perform(post("/api/v1/sites/PLANT-A/assets")
                        .contentType(MediaType.APPLICATION_JSON)
                        .content("""
                                {
                                  "assetId": "MILL-07",
                                  "machineType": "CNC_MILL",
                                  "firmwareVersion": "1.0.0",
                                  "sensors": []
                                }
                                """))
                .andExpect(status().isBadRequest());
    }

    @Test
    void rejectsAnUnrecognisedMachineType() throws Exception {
        mockMvc.perform(post("/api/v1/sites/PLANT-A/assets")
                        .contentType(MediaType.APPLICATION_JSON)
                        .content("""
                                {
                                  "assetId": "MYSTERY-01",
                                  "machineType": "TELEPORTER",
                                  "firmwareVersion": "1.0.0",
                                  "sensors": ["vibration"]
                                }
                                """))
                .andExpect(status().isBadRequest());
    }
}
